import logging
import math
import multiprocessing
import time

import carla
import numpy as np

logger = logging.getLogger(__name__)

CAMERA_SHAPE = (320, 480, 3)


def create_handler(remote, connect_timeout_sec=2, on_image=(lambda x: x)):
    carla_host, carla_port = remote, 2000
    if ':' in carla_host:
        host, port = carla_host.split(':')
        carla_host, carla_port = host, int(port)
    carla_client = carla.Client(carla_host, carla_port)
    carla_client.set_timeout(float(connect_timeout_sec))
    world = carla_client.get_world()
    return CarlaHandler(world=world, camera_callback=on_image)


class CarlaHandler(object):

    def __init__(self, **kwargs):
        self._world = kwargs.get('world')
        self._camera_callback = kwargs.get('camera_callback')
        self._actor = None
        self._image_shape = CAMERA_SHAPE
        self._sensors = []
        self._actor_lock = multiprocessing.Lock()
        self._actor_last_location = None
        self._actor_distance_traveled = 0.
        self._spawn_index = 0
        self._vehicle_tick = self._world.on_tick(lambda x: self.tick(x))

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
        im_height, im_width = self._image_shape[:2]
        camera_bp.set_attribute('image_size_x', '{}'.format(im_width))
        camera_bp.set_attribute('image_size_y', '{}'.format(im_height))
        # camera_bp.set_attribute('fov', '150')
        # Set the time in seconds between sensor captures
        camera_bp.set_attribute('sensor_tick', "{:2.2f}".format(1. / 100))
        # Provide the position of the sensor relative to the vehicle.
        # camera_transform = carla.Transform(carla.Location(x=0.8, z=1.7))
        camera_transform = carla.Transform(carla.Location(x=1.25, z=1.4))
        # Tell the world to spawn the sensor, don't forget to attach it to your vehicle actor.
        camera = self._world.spawn_actor(camera_bp, camera_transform, attach_to=self._actor)
        self._sensors.append(camera)
        # Subscribe to the sensor stream by providing a callback function, this function is
        # called each time a new image is generated by the sensor.
        camera.listen(lambda data: self._on_camera(data))
        self._reset_agent_travel()

    def _on_camera(self, data):
        img = np.frombuffer(data.raw_data, dtype=np.dtype("uint8"))
        _height, _width = self._image_shape[:2]
        img = np.reshape(img, (_height, _width, 4))  # To bgr_a format.
        img = img[:, :, :3]  # The image standard is hwc bgr.
        self._camera_callback(img)

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

    def state(self):
        x, y = self._position()
        return dict(x_coordinate=x,
                    y_coordinate=y,
                    heading=self._heading(),
                    velocity=self._velocity(),
                    time=time.time())

    def start(self):
        self._reset()

    def quit(self):
        self._world.remove_on_tick(self._vehicle_tick)
        self._destroy()

    def tick(self, _):
        if self._actor is not None and self._actor.is_alive:
            with self._actor_lock:
                location = self._actor.get_location()
                if self._actor_last_location is not None:
                    _x, _y = self._actor_last_location
                    self._actor_distance_traveled += math.sqrt((location.x - _x) ** 2 + (location.y - _y) ** 2)
                self._actor_last_location = (location.x, location.y)

    def drive(self, cmd):
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
