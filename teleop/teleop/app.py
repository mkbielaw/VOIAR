#!/usr/bin/env python
from __future__ import absolute_import

import argparse
import asyncio
import concurrent.futures
import configparser
import glob
import multiprocessing
import signal
from concurrent.futures import ThreadPoolExecutor

import tornado.ioloop
import tornado.web
import user_agents  # Check in the request header if it is a phone or not
from byodr.utils import Application, ApplicationExit, hash_dict
from byodr.utils.ipc import CameraThread, JSONPublisher, JSONZmqClient, json_collector
from byodr.utils.navigate import FileSystemRouteDataSource, ReloadableDataSource
from byodr.utils.option import parse_option
from htm.plot_training_sessions_map.draw_training_sessions import draw_training_sessions
from logbox.app import LogApplication, PackageApplication
from logbox.core import MongoLogBox, SharedState, SharedUser
from logbox.web import DataTableRequestHandler, JPEGImageRequestHandler
from pymongo import MongoClient
from tornado import ioloop, web
from tornado.httpserver import HTTPServer
from tornado.platform.asyncio import AnyThreadEventLoopPolicy

from .getSSID import fetch_ssid
from .server import *

logger = logging.getLogger(__name__)

log_format = "%(levelname)s: %(asctime)s %(filename)s %(funcName)s %(message)s"

signal.signal(signal.SIGINT, lambda sig, frame: _interrupt())
signal.signal(signal.SIGTERM, lambda sig, frame: _interrupt())

quit_event = multiprocessing.Event()
# A thread pool to run blocking tasks
thread_pool = ThreadPoolExecutor()
current_throttle = 0

# Variable in use for the following
stats = None


def _interrupt():
    logger.info("Received interrupt, quitting.")
    quit_event.set()


def _load_nav_image(fname):
    image = cv2.imread(fname)
    image = cv2.resize(image, (160, 120))
    image = image.astype(np.uint8)
    return image


class TeleopApplication(Application):
    def __init__(self, event, config_dir=os.getcwd()):
        """set up configuration directory and a configuration file path

        Args:
            event: allow for thread-safe signaling between processes or threads, indicating when to gracefully shut down or quit certain operations. The TeleopApplication would use this event to determine if it should stop or continue its operations.

            config_dir: specified by the command-line argument --config in the main function. Its default value is set to os.getcwd(), meaning if it's not provided externally, it'll default to the current working directory where the script is run. When provided, this directory is where the application expects to find its .ini configuration files.
        """
        super(TeleopApplication, self).__init__(quit_event=event)
        self._config_dir = config_dir
        self._user_config_file = os.path.join(self._config_dir, "config.ini")
        self._config_hash = -1
        self.rut_ip = None

    def _check_user_config(self):
        _candidates = glob.glob(os.path.join(self._config_dir, "*.ini"))
        for file_path in _candidates:
            # Extract the filename from the path
            file_name = os.path.basename(file_path)
            if file_name == "config.ini":
                self._user_config_file = file_path

    def _config(self):
        parser = SafeConfigParser()
        [parser.read(_f) for _f in glob.glob(os.path.join(self._config_dir, "*.ini"))]
        cfg = dict(parser.items("teleop")) if parser.has_section("teleop") else {}
        return cfg

    def get_user_config_file(self):
        return self._user_config_file

    def read_user_config(self):
        """
        Reads the configuration file, flattens the configuration sections and keys,
        and initializes components with specific configuration values.
        """
        config = configparser.ConfigParser()
        config.read(self.get_user_config_file())

        # Flatten the configuration sections and keys into a single dictionary
        config_dict = {f"{section}.{option}": value for section in config.sections() for option, value in config.items(section)}

        errors = []
        # print(config_dict)
        # Use the flattened config dictionary as **kwargs to parse_option
        # A close implementation for how the parse_option is called in the internal_start function for each service.
        self.rut_ip = parse_option("vehicle.gps.provider.host", str, "192.168.1.1", errors, **config_dict)

        if errors:
            for error in errors:
                print(f"Configuration error: {error}")

    def setup(self):
        if self.active():
            self._check_user_config()
            self.read_user_config()
            _config = self._config()
            _hash = hash_dict(**_config)
            if _hash != self._config_hash:
                self._config_hash = _hash


class RunGetSSIDPython(tornado.web.RequestHandler):
    """Run a python script to get the SSID of current robot"""

    async def get(self):
        try:
            # Use the IOLoop to run fetch_ssid in a thread
            loop = tornado.ioloop.IOLoop.current()

            config = configparser.ConfigParser()
            config.read("/config/config.ini")
            front_camera_ip = config["camera"]["front.camera.ip"]
            parts = front_camera_ip.split(".")
            network_prefix = ".".join(parts[:3])
            router_IP = f"{network_prefix}.1"
            # name of python function to run, ip of the router, ip of SSH, username, password, command to get the SSID
            ssid = await loop.run_in_executor(
                None,
                fetch_ssid,
                router_IP,
                22,
                "root",
                "Modem001",
                "uci get wireless.@wifi-iface[0].ssid",
            )

            logger.info(f"SSID of current robot: {ssid}")
            self.write(ssid)
        except Exception as e:
            logger.error(f"Error fetching SSID of current robot: {e}")
            self.set_status(500)
            self.write("Error fetching SSID of current robot.")
        self.finish()


class Index(tornado.web.RequestHandler):
    """The Main landing page"""

    def get(self):
        user_agent_str = self.request.headers.get("User-Agent")
        user_agent = user_agents.parse(user_agent_str)

        if user_agent.is_mobile:
            # if user is on mobile, redirect to the mobile page
            logger.info("User is operating through mobile phone. Redirecting to the mobile UI")
            self.redirect("/mobile_controller_ui")
        else:
            # else render the index page
            self.render("../htm/templates/index.html")


class UserMenu(tornado.web.RequestHandler):
    """The user menu setting page"""

    def get(self):
        print("navigating to the user menu page")
        self.render("../htm/templates/user_menu.html")


class MobileControllerUI(tornado.web.RequestHandler):
    """Load the user interface for mobile controller"""

    def get(self):
        self.render("../htm/templates/mobile_controller_ui.html")


def main():
    """
    It parses command-line arguments for configuration details and sets up various components:
      - MongoDB connection and indexing.
      - Route data source for navigation.
      - Camera threads for front and rear cameras.
      - JSON collectors for the pilot, vehicle, and inference data.
      - LogBox setup for logging.
      - Threaded applications for logging and packaging data.

    It initializes multiple threads for various components, including cameras, pilot, vehicle, inference, and logging.

    JSON publishers are set up for teleop data and chatter data.

    """

    global stats

    parser = argparse.ArgumentParser(description="Teleop sockets server.")
    parser.add_argument("--name", type=str, default="none", help="Process name.")
    parser.add_argument("--config", type=str, default="/config", help="Config directory path.")
    parser.add_argument("--routes", type=str, default="/routes", help="Directory with the navigation routes.")
    parser.add_argument("--sessions", type=str, default="/sessions", help="Sessions directory.")
    args = parser.parse_args()

    # The mongo client is thread-safe and provides for transparent connection pooling.
    _mongo = MongoLogBox(MongoClient())
    _mongo.ensure_indexes()

    route_store = ReloadableDataSource(FileSystemRouteDataSource(directory=args.routes, fn_load_image=_load_nav_image, load_instructions=False))
    route_store.load_routes()

    application = TeleopApplication(event=quit_event, config_dir=args.config)
    application.setup()

    camera_front = CameraThread(url="ipc:///byodr/camera_0.sock", topic=b"aav/camera/0", event=quit_event)
    camera_rear = CameraThread(url="ipc:///byodr/camera_1.sock", topic=b"aav/camera/1", event=quit_event)
    pilot = json_collector(url="ipc:///byodr/pilot.sock", topic=b"aav/pilot/output", event=quit_event, hwm=20)
    following = json_collector(
        url="ipc:///byodr/following.sock",
        topic=b"aav/following/controls",
        event=quit_event,
        hwm=1,
    )
    vehicle = json_collector(url="ipc:///byodr/vehicle.sock", topic=b"aav/vehicle/state", event=quit_event, hwm=20)
    inference = json_collector(url="ipc:///byodr/inference.sock", topic=b"aav/inference/state", event=quit_event, hwm=20)

    logbox_user = SharedUser()
    logbox_state = SharedState(
        channels=(camera_front, (lambda: pilot.get()), (lambda: vehicle.get()), (lambda: inference.get())),
        hz=16,
    )
    log_application = LogApplication(_mongo, logbox_user, logbox_state, event=quit_event, config_dir=args.config)
    package_application = PackageApplication(_mongo, logbox_user, event=quit_event, hz=0.100, sessions_dir=args.sessions)

    def send_command():
        global stats
        config = SafeConfigParser()
        config.read(application.get_user_config_file())
        front_camera_ip = config.get(
            "camera", "front.camera.ip", fallback="192.168.1.64"
        )

        camera_control = CameraControl(
            f"http://{front_camera_ip}:80", "user1", "HaikuPlot876"
        )

        while True:
            while stats == "Start Following":
                ctrl = following.get()
                if ctrl is not None:
                    print(ctrl)
                    ctrl["time"] = timestamp()
                    if ctrl["camera_pan"] is not None:
                        camera_control.adjust_ptz(pan=ctrl["camera_pan"], tilt=0)
                        ctrl["camera_pan"] = 0
                    # will always send the current azimuth for the bottom camera while following is working
                    camera_azimuth, camera_elevation = camera_control.get_ptz_status()
                    chatter.publish({"camera_azimuth": camera_azimuth})
                    print(camera_azimuth)

                    teleop_publish(ctrl)

    logbox_thread = threading.Thread(target=log_application.run)
    package_thread = threading.Thread(target=package_application.run)
    follow_thread = threading.Thread(target=send_command)

    threads = [
        camera_front,
        camera_rear,
        pilot,
        following,
        vehicle,
        inference,
        logbox_thread,
        package_thread,
        follow_thread
    ]

    if quit_event.is_set():
        return 0

    [t.start() for t in threads]

    teleop_publisher = JSONPublisher(url="ipc:///byodr/teleop.sock", topic="aav/teleop/input")
    # external_publisher = JSONPublisher(url='ipc:///byodr/external.sock', topic='aav/external/input')
    chatter = JSONPublisher(url="ipc:///byodr/teleop_c.sock", topic="aav/teleop/chatter")
    zm_client = JSONZmqClient(urls=["ipc:///byodr/pilot_c.sock", "ipc:///byodr/inference_c.sock", "ipc:///byodr/vehicle_c.sock", "ipc:///byodr/relay_c.sock", "ipc:///byodr/camera_c.sock"])

    def on_options_save():
        chatter.publish(dict(time=timestamp(), command="restart"))
        application.setup()

    def list_process_start_messages():
        return zm_client.call(dict(request="system/startup/list"))

    def list_service_capabilities():
        return zm_client.call(dict(request="system/service/capabilities"))

    def get_navigation_image(image_id):
        return route_store.get_image(image_id)

    def throttle_control(cmd):
        global current_throttle  # The throttle value that we will send in this iteration of the function. Starts as 0.0
        global stats  # Checking if Following is running, so that the throttle control does not send commands at the same time as following
        throttle_change_step = 0.1  # Always 0.1

        # Is it ugly, i know
        if cmd.get("mobileInferenceState") == "true" or cmd.get("mobileInferenceState") == "auto" or cmd.get("mobileInferenceState") == "train":
            # cmd.pop("mobileInferenceState")
            teleop_publish(cmd)
        # Sometimes the JS part sends over a command with no throttle (When we are on the main page of teleop, without a controller, or when we want to brake urgently)
        else:
            if "throttle" in cmd and stats != "Start Following":
                # First key of the dict, checking if its throttle or steering
                first_key = next(iter(cmd))
                # Getting the throttle value of the user's finger. Thats the throttle value we want to end up at
                target_throttle = float(cmd.get("throttle"))

                # If steering is the 1st key of the dict, then it means the user gives no throttle input
                if first_key == "steering":
                    # Getting the sign of the previous throttle, so that we know if we have to add or subtract the step when braking
                    braking_sign = -1 if current_throttle < 0 else 1

                    # Decreasing or increasing the throttle by each iteration, by the step we have defined.
                    # Dec or Inc depends on if we were going forwards or backwards
                    current_throttle = current_throttle - (braking_sign * throttle_change_step)

                    # Capping the value at 0 so that the robot does not move while idle
                    if braking_sign > 0 and current_throttle < 0:
                        current_throttle = 0.0

                # If throttle is the 1st key of the dict, then it means the user gives throttle input
                else:
                    # Getting the sign of the target throttle, so that we know if we have to add or subtract the step when accelerating
                    accelerate_sign = 0
                    if target_throttle < current_throttle:
                        accelerate_sign = -1
                    elif target_throttle > current_throttle:
                        accelerate_sign = 1

                    # Decreasing or increasing the throttle by each iteration, by the step we have defined.
                    # Dec or Inc depends on if we want to go forwards or backwards
                    current_throttle = current_throttle + (accelerate_sign * throttle_change_step)

                    # Capping the value at the value of the user's finger so that the robot does not move faster than the user wants
                    if (accelerate_sign > 0 and current_throttle > target_throttle) or (accelerate_sign < 0 and current_throttle < target_throttle):
                        current_throttle = target_throttle

                # Sending commands to Coms/Pilot
                cmd["throttle"] = current_throttle
                teleop_publish(cmd)

            # When we receive commands without throttle in them, we reset the current throttle value to 0
            else:
                current_throttle = 0
                teleop_publish({"steering": 0.0, "throttle": 0, "time": timestamp(), "navigator": {"route": None}, "button_b": 1})

    def teleop_publish(cmd):
        # We are the authority on route state.
        cmd["navigator"] = dict(route=route_store.get_selected_route())
        # print(cmd)
        # print(vehicle.get())
        teleop_publisher.publish(cmd)

    asyncio.set_event_loop_policy(AnyThreadEventLoopPolicy())
    asyncio.set_event_loop(asyncio.new_event_loop())

    def teleop_publish_to_following(cmd):
        global stats

        chatter.publish(cmd)
        stats = cmd["following"]

    io_loop = ioloop.IOLoop.instance()
    _conditional_exit = ApplicationExit(quit_event, lambda: io_loop.stop())
    _periodic = ioloop.PeriodicCallback(lambda: _conditional_exit(), 5e3)
    _periodic.start()

    try:
        main_app = web.Application(
            [
                # Landing page
                (r"/", Index),
                (r"/user_menu", UserMenu),  # Navigate to user menu settings page
                (
                    r"/mobile_controller_ui",
                    MobileControllerUI,
                ),  # Navigate to Mobile controller UI
                (
                    # Getting the commands from the mobile controller (commands are sent in JSON)
                    r"/ws/send_mobile_controller_commands",
                    MobileControllerCommands,
                    dict(fn_control=throttle_control),
                ),
                (r"/latest_image", LatestImageHandler, {"path": "/byodr/yolo_person"}),
                (
                    r"/switch_following",
                    FollowingHandler,
                    dict(fn_control=teleop_publish_to_following),
                ),
                # Run python script to get the SSID for the current segment
                (r"/run_get_SSID", RunGetSSIDPython),
                (
                    r"/ws/switch_confidence",
                    ConfidenceHandler,
                    dict(
                        inference_s=inference,
                        vehicle_s=vehicle,
                    ),
                ),
                (
                    r"/api/datalog/event/v10/table",
                    DataTableRequestHandler,
                    dict(mongo_box=_mongo),
                ),
                (
                    r"/api/datalog/event/v10/image",
                    JPEGImageRequestHandler,
                    dict(mongo_box=_mongo),
                ),  # Get the commands from the controller in normal UI
                (r"/ws/ctl", ControlServerSocket, dict(fn_control=throttle_control)),
                (
                    r"/ws/log",
                    MessageServerSocket,
                    dict(fn_state=(lambda: (pilot.peek(), vehicle.peek(), inference.peek()))),
                ),
                (
                    r"/ws/cam/front",
                    CameraMJPegSocket,
                    dict(image_capture=(lambda: camera_front.capture())),
                ),
                (
                    r"/ws/cam/rear",
                    CameraMJPegSocket,
                    dict(image_capture=(lambda: camera_rear.capture())),
                ),
                (
                    r"/ws/nav",
                    NavImageHandler,
                    dict(fn_get_image=(lambda image_id: get_navigation_image(image_id))),
                ),
                (
                    # Get or save the options for the user
                    r"/teleop/user/options",
                    ApiUserOptionsHandler,
                    dict(
                        user_options=(UserOptions(application.get_user_config_file())),
                        fn_on_save=on_options_save,
                    ),
                ),
                (
                    r"/teleop/system/state",
                    JSONMethodDumpRequestHandler,
                    dict(fn_method=list_process_start_messages),
                ),
                (
                    r"/teleop/system/capabilities",
                    JSONMethodDumpRequestHandler,
                    dict(fn_method=list_service_capabilities),
                ),
                (
                    r"/teleop/navigation/routes",
                    JSONNavigationHandler,
                    dict(route_store=route_store),
                ),
                (
                    # Path to where the static files are stored (JS,CSS, images)
                    r"/(.*)",
                    web.StaticFileHandler,
                    {"path": os.path.join(os.path.sep, "app", "htm")},
                ),
            ]
        )
        http_server = HTTPServer(main_app, xheaders=True)
        port_number = 8080
        http_server.bind(port_number)
        http_server.start()
        logger.info(f"Teleop web services starting on port {port_number}.")
        io_loop.start()
    except KeyboardInterrupt:
        quit_event.set()
    finally:
        _mongo.close()
        _periodic.stop()

    route_store.quit()

    logger.info("Waiting on threads to stop.")
    [t.join() for t in threads]


if __name__ == "__main__":
    logging.basicConfig(format=log_format, datefmt="%Y%m%d:%H:%M:%S %p %Z")
    logging.getLogger().setLevel(logging.INFO)
    main()
