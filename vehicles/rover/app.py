import argparse
import collections
import json
import logging
import multiprocessing
import signal
import sys
import time

import numpy as np
import rospy
from geometry_msgs.msg import Twist, TwistStamped
from jsoncomment import JsonComment

from byodr.utils.ipc import ReceiverThread, JSONPublisher, ImagePublisher
from video import GstRawSource

logger = logging.getLogger(__name__)
log_format = '%(levelname)s: %(filename)s %(funcName)s %(message)s'

quit_event = multiprocessing.Event()

CAMERA_SHAPE = (240, 320, 3)
CH_NONE, CH_THROTTLE, CH_STEERING, CH_BOTH = (0, 1, 2, 3)
CTL_LAST = 0

signal.signal(signal.SIGINT, lambda sig, frame: _interrupt())
signal.signal(signal.SIGTERM, lambda sig, frame: _interrupt())


def _interrupt():
    logger.info("Received interrupt, quitting.")
    quit_event.set()


class RosGate(object):
    """
    """

    def __init__(self, connect=True):
        """
        """
        # Keep half a second worth of readings.
        self._odometer_deque = collections.deque(maxlen=5)

        if connect:
            rospy.Subscriber("roy_teleop/sensor/odometer", TwistStamped, self._update_odometer)
            self._pub = rospy.Publisher('roy_teleop/command/drive', Twist, queue_size=1)

    def _update_odometer(self, message):
        # Comes in at 10Hz.
        counter = int(message.twist.linear.y)
        self._odometer_deque.append(counter)

    def publish(self, channel=CH_NONE, throttle=0., steering=0., control=CTL_LAST, button=0):
        if not quit_event.is_set():
            # The button state does not need to go to ros but currently there is no separate state holder (server side)
            # for the button state per timestamp.
            # Scale the throttle as a replacement for a 'mechanical' throttle maximizer.
            twist = Twist()
            twist.angular.x, twist.angular.z, twist.linear.x, twist.linear.z = (channel, steering, control, throttle)
            twist.linear.y = button
            self._pub.publish(twist)

    def get_odometer_value(self):
        # Convert to meters / second. Scale is determined by the vehicle.
        # Reverse measurements not currently possible.
        return max(0, sum(list(self._odometer_deque)) / 20.)


class FakeGate(RosGate):
    def __init__(self):
        super(FakeGate, self).__init__(connect=False)

    def publish(self, channel=CH_NONE, throttle=0., steering=0., control=CTL_LAST, button=0):
        pass


class TwistHandler(object):
    def __init__(self, config_file, ros_gate):
        super(TwistHandler, self).__init__()
        self._gate = ros_gate
        self._steer_calibration_shift = None
        self._throttle_calibration_shift = None
        try:
            with open(config_file, 'r') as cfg_file:
                cfg = JsonComment(json).loads(cfg_file.read())
            _steer_shift = float(cfg.get('platform.calibrate.steer.shift'))
            _throttle_shift = float(cfg.get('platform.calibrate.throttle.shift'))
            self._steer_calibration_shift = _steer_shift
            self._throttle_calibration_shift = _throttle_shift
            self._throttle_forward_scale = float(cfg.get('platform.throttle.forward.scale'))
            self._throttle_backward_scale = float(cfg.get('platform.throttle.backward.scale'))
            logger.info("Calibration steer, throttle is {:2.2f}, {:2.2f}.".format(_steer_shift, _throttle_shift))
        except TypeError:
            _sub_dict = {k: v for k, v in cfg.items() if k.startswith('platform')}
            raise AssertionError("Please specify valid calibration values - not '{}'.".format(_sub_dict))

    def _scale(self, _throttle, _steering):
        # First shift.
        _steering += self._steer_calibration_shift
        _throttle += self._throttle_calibration_shift
        # Then scale.
        _throttle = (_throttle * self._throttle_backward_scale) if _throttle < 0 else (_throttle * self._throttle_forward_scale)
        # Protect boundaries.
        _steering = int(max(-1, min(1, _steering)) * 180 / 2 + 90)
        _throttle = int(max(-1, min(1, _throttle)) * 180 / 2 + 90)
        return _throttle, _steering

    def _drive(self, steering, throttle):
        try:
            throttle, steering = self._scale(throttle, steering)
            self._gate.publish(steering=steering, throttle=throttle)
        except Exception as e:
            logger.error("{}".format(e))

    def state(self):
        x, y = 0, 0
        return dict(x_coordinate=x,
                    y_coordinate=y,
                    heading=0,
                    velocity=self._gate.get_odometer_value(),
                    time=time.time())

    def noop(self):
        self._drive(steering=0, throttle=0)

    def drive(self, cmd):
        if cmd is not None:
            self._drive(steering=cmd.get('steering'), throttle=cmd.get('throttle'))


def _ros_init():
    # Ros replaces the root logger - add a new handler after ros initialisation.
    rospy.init_node('rover', disable_signals=False, anonymous=True, log_level=rospy.INFO)
    console_handler = logging.StreamHandler(stream=sys.stdout)
    console_handler.setFormatter(logging.Formatter(log_format))
    logging.getLogger().addHandler(console_handler)
    logging.getLogger().setLevel(logging.INFO)
    rospy.on_shutdown(lambda: quit_event.set())


def main():
    parser = argparse.ArgumentParser(description='Rover main.')
    parser.add_argument('--config', type=str, required=True, help='Config file location.')
    parser.add_argument('--clock', type=int, required=True, help='Main loop frequency in hz.')
    parser.add_argument('--dry', default=False, type=lambda x: (str(x).lower() == 'true'), help='Dry run')

    args = parser.parse_args()
    if args.dry:
        gate = FakeGate()
    else:
        _ros_init()
        gate = RosGate()
        logger.info("ROS gate started.")

    state_publisher = JSONPublisher(url='ipc:///byodr/vehicle.sock', topic='aav/vehicle/state')
    image_publisher = ImagePublisher(url='ipc:///byodr/camera.sock', topic='aav/camera/0')

    def _image(_b):
        image_publisher.publish(np.fromstring(_b.extract_dup(0, _b.get_size()), dtype=np.uint8).reshape(CAMERA_SHAPE))

    _url = "rtspsrc " \
           "location=rtsp://user1:HelloUser1@192.168.50.64:554/Streaming/Channels/102 " \
           "latency=0 drop-on-latency=true ! queue ! " \
           "rtph264depay ! h264parse ! queue ! avdec_h264 ! videoconvert ! " \
           "videoscale ! video/x-raw,width={},height={},format=BGR ! queue".format(CAMERA_SHAPE[1], CAMERA_SHAPE[0])
    gst_source = GstRawSource(fn_callback=_image, command=_url)
    gst_source.open()

    vehicle = TwistHandler(config_file=args.config, ros_gate=gate)
    threads = []
    pilot = ReceiverThread(url='ipc:///byodr/pilot.sock', topic=b'aav/pilot/output', event=quit_event)
    threads.append(pilot)
    [t.start() for t in threads]

    _hz = args.clock
    _period = 1. / _hz
    logger.info("Running at {} hz.".format(_hz))
    while not quit_event.is_set():
        command = pilot.get_latest()
        _command_time = 0 if command is None else command.get('time')
        _command_age = time.time() - _command_time
        _on_time = _command_age < (2 * _period)
        if _on_time:
            vehicle.drive(command)
        else:
            vehicle.noop()
        state_publisher.publish(vehicle.state())
        time.sleep(_period)

    logger.info("Waiting on threads to stop.")
    gst_source.close()

    logger.info("Waiting on threads to stop.")
    [t.join() for t in threads]


if __name__ == "__main__":
    logging.basicConfig(format=log_format)
    logging.getLogger().setLevel(logging.DEBUG)
    main()
