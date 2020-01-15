import argparse
import json
import logging
import math
import multiprocessing
import sys
from collections import deque

import carla
import numpy as np
import rospy
from std_msgs.msg import String as RosString

logger = logging.getLogger(__name__)
log_format = '%(levelname)s: %(filename)s %(funcName)s %(message)s'

quit_event = multiprocessing.Event()


class CarlaHandler(object):

    def __init__(self, world):
        self._world = world
        self._actor = None
        self._image_shape = (320, 480, 3)
        self._images = deque(maxlen=2)
        self._sensors = []
        self._actor_lock = multiprocessing.Lock()
        self._actor_last_location = None
        self._actor_distance_traveled = 0.
        self._spawn_index = 0

    def _reset_agent_travel(self):
        logger.info("Actor distance traveled is {:8.3f}.".format(self._actor_distance_traveled))
        self._actor_distance_traveled = 0.
        self._actor_last_location = None

    def _destroy(self):
        if self._actor is not None and self._actor.is_alive:
            self._actor.destroy()
        for sensor in self._sensors:
            if sensor.is_alive:
                sensor.destroy()

    def _reset(self):
        logger.info('Resetting ...')
        self._destroy()
        #
        blueprint_library = self._world.get_blueprint_library()
        vehicle_bp = blueprint_library.find('vehicle.tesla.model3')
        spawn_points = self._world.get_map().get_spawn_points()
        spawn_idx = self._spawn_index + 1 if (self._spawn_index + 1) < len(spawn_points) else 0
        spawn_point = spawn_points[spawn_idx]
        logger.info("Spawn point is '{}'.".format(spawn_point))
        self._actor = self._world.spawn_actor(vehicle_bp, spawn_point)
        # Attach the camera's - defaults at https://carla.readthedocs.io/en/latest/cameras_and_sensors/.
        camera_bp = self._world.get_blueprint_library().find('sensor.camera.rgb')
        # Modify the attributes of the blueprint to set image resolution and field of view.
        camera_bp.set_attribute('image_size_x', '480')
        camera_bp.set_attribute('image_size_y', '320')
        # camera_bp.set_attribute('fov', '150')
        # Set the time in seconds between sensor captures
        camera_bp.set_attribute('sensor_tick', "{:2.2f}".format(1. / 50))
        # Provide the position of the sensor relative to the vehicle.
        # camera_transform = carla.Transform(carla.Location(x=0.8, z=1.7))
        camera_transform = carla.Transform(carla.Location(x=1.25, z=1.4))
        # Tell the world to spawn the sensor, don't forget to attach it to your vehicle actor.
        camera = self._world.spawn_actor(camera_bp, camera_transform, attach_to=self._actor)
        self._sensors.append(camera)
        # Subscribe to the sensor stream by providing a callback function, this function is
        # called each time a new image is generated by the sensor.
        camera.listen(lambda data: self._images.appendleft(data))
        self._reset_agent_travel()

    def _get_camera(self):
        # noinspection PyBroadException
        try:
            img = np.frombuffer(self._images[0].raw_data, dtype=np.dtype("uint8"))
            _height, _width = self._image_shape[:2]
            img = np.reshape(img, (_height, _width, 4))  # To bgr_a format.
            # The image standard is hwc bgr.
            img = img[:, :, :3]
            return img
        except IndexError:
            # No images received as of yet.
            pass
        except Exception as e:
            logger.warn(e)
        return np.zeros(shape=self._image_shape, dtype=np.uint8)

    def _carla_vel(self):
        velocity = self._actor.get_velocity()
        return math.sqrt(velocity.x ** 2 + velocity.y ** 2 + velocity.z ** 2) / 1.6 / 3.6

    def _position(self):
        location = None if self._actor is None else self._actor.get_location()
        return (-1, -1) if location is None else (location.x, location.y)

    def _heading(self):
        return 0 if self._actor is None else self._actor.get_transform().rotation.yaw

    def _velocity(self):
        return 0 if self._actor is None else self._carla_vel()

    def start(self):
        self._reset()

    def quit(self):
        self._destroy()

    def on_tick(self, _):
        if self._actor is not None and self._actor.is_alive:
            with self._actor_lock:
                location = self._actor.get_location()
                if self._actor_last_location is not None:
                    _x, _y = self._actor_last_location
                    self._actor_distance_traveled += math.sqrt((location.x - _x) ** 2 + (location.y - _y) ** 2)
                self._actor_last_location = (location.x, location.y)

    # def on_ctl_switch(self, control, **kwargs):
    #     if control == Control.CTL_JOYSTICK:
    #         self._spawn_location = (0, None)
    #     if self._actor is not None:
    #         self._actor.set_autopilot(control == Control.CTL_AUTOPILOT)
    #     with self._actor_lock:
    #         self._reset_agent_travel()

    #                 if blob.control == Control.CTL_AUTOPILOT:
    #                     vehicle_control = self._actor.get_control()
    #                     blob.throttle = vehicle_control.throttle
    #                     blob.steering = vehicle_control.steer
    #                     blob.desired_speed = self._carla_vel()
    #                     blob.road_speed = blob.desired_speed

    def get_state(self):
        x, y = self._position()
        return dict(x_coordinate=x, y_coordinate=y, heading=self._heading(), velocity=self._velocity())

    def on_drive(self, cmd):
        if self._actor is not None:
            try:
                steering, throttle = cmd.get('steering'), cmd.get('throttle')
                control = carla.VehicleControl()
                control.steer = steering
                if throttle > 0:
                    control.throttle = throttle
                else:
                    control.brake = abs(throttle)
                self._actor.apply_control(control)
            except Exception as e:
                logger.error("{}".format(e))


def _ros_init():
    # Ros replaces the root logger - add a new handler after ros initialisation.
    rospy.init_node('carla', disable_signals=False, anonymous=True, log_level=rospy.DEBUG)
    console_handler = logging.StreamHandler(stream=sys.stdout)
    console_handler.setFormatter(logging.Formatter(log_format))
    console_handler.setLevel(logging.DEBUG)
    logging.getLogger().addHandler(console_handler)
    logging.getLogger().setLevel(logging.DEBUG)
    rospy.on_shutdown(lambda: quit_event.set())


def main():
    parser = argparse.ArgumentParser(description='Carla vehicle client.')
    parser.add_argument('--remote', type=str, required=True, help='Carla server remote host:port')
    args = parser.parse_args()

    carla_host, carla_port = args.remote, 2000
    if ':' in carla_host:
        host, port = carla_host.split(':')
        carla_host, carla_port = host, int(port)

    carla_client = carla.Client(carla_host, carla_port)
    carla_client.set_timeout(2.)  # seconds
    world = carla_client.get_world()
    vehicle = CarlaHandler(world=world)
    vehicle.start()
    callback_id = world.on_tick(lambda ts: vehicle.on_tick(ts))

    _ros_init()
    vehicle_topic = rospy.Publisher('aav/vehicle/state/blob', RosString, queue_size=1)
    # camera_topic = rospy.Publisher('aav/vehicle/camera/0', RosString, queue_size=1)

    def on_drive(data):
        vehicle.on_drive(json.loads(data.data))
        vehicle_topic.publish(json.dumps(vehicle.get_state()))

    rospy.Subscriber('aav/pilot/command/blob', RosString, on_drive)
    rospy.spin()

    # Done.
    logger.info("Waiting on carla to quit.")
    world.remove_on_tick(callback_id)
    vehicle.quit()


if __name__ == "__main__":
    logging.basicConfig(format=log_format)
    logging.getLogger().setLevel(logging.DEBUG)
    main()
