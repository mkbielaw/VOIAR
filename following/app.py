import os 
import glob 
import configparser 
import logging 
import multiprocessing 
import math 
from ultralytics import YOLO 
import cv2 
from byodr.utils import timestamp 
from byodr.utils.ipc import JSONPublisher, json_collector 
from byodr.utils.option import parse_option 
 
# Constants 
SCREEN_CENTER = 320 
CENTER_OFFSET = 0 
SECOND_OFFSET = 80 
THIRD_OFFSET = 70 
START_HEIGHT = 340 
UNSAFE_HEIGHT = 360 
MAX_HUMAN_ABSENCE_FRAMES = 3 
MIN_CLEAR_PATH_FRAMES = 3 
SMOOTH_CONTROL_STEP = 0.1 
 
 
class FollowingController: 
    def __init__(self, model_path, config_path="/config"): 
        self.quit_event = multiprocessing.Event() 
        self.model = YOLO(model_path) 
        self.no_human_counter = 0 
        self.clear_path = 4 
        self.logger = self.setup_logger() 
        self.config = self.load_config(config_path) 
        self.teleop = self.setup_teleop_receiver() 
        self.publisher = self.setup_publisher() 
        self.current_throttle = 0 
        self.current_steering = 0 
        self.image_counter = 0 
        self.image_save_path = "/byodr/yolo_person" 
        os.makedirs(self.image_save_path, exist_ok=True) 
 
    def reset_tracking_session(self): 
        """Reset the image counter and clear all images in the directory.""" 
        self.image_counter = 0 
        for existing_file in os.listdir(self.image_save_path): 
            os.remove(os.path.join(self.image_save_path, existing_file)) 
        self.logger.info("Tracking session reset: Image counter zeroed and folder cleared.") 
 
    def setup_logger(self): 
        logging.basicConfig(format="%(levelname)s: %(asctime)s %(filename)s %(funcName)s %(message)s", datefmt="%Y%m%d:%H:%M:%S %p %Z") 
        logger = logging.getLogger(__name__) 
        logger.setLevel(logging.INFO) 
        return logger 
 
    def track_and_save_image(self, result): 
        """Tracks objects in video stream and saves the latest image with annotations.""" 
        # https://github.com/ultralytics/ultralytics/issues/1696#issuecomment-1948021841 
        full_annotated_image = result.plot(show=False, pil=False)  # Ensuring it returns a numpy array 
        full_annotated_image = cv2.cvtColor(full_annotated_image, cv2.COLOR_RGB2BGR)  # Convert RGB to BGR for OpenCV 
        filename = os.path.join(self.image_save_path, f"image_{self.image_counter}.jpg") 
        cv2.imwrite(filename, full_annotated_image) 
        self.image_counter += 1 
 
        # Check the number of images in the directory and delete the oldest if more than 10 
        all_images = sorted(os.listdir(self.image_save_path), key=lambda x: os.path.getctime(os.path.join(self.image_save_path, x))) 
 
        if len(all_images) > 10: 
            oldest_image = all_images[0] 
            os.remove(os.path.join(self.image_save_path, oldest_image)) 
 
    def load_config(self, path): 
        parser = configparser.ConfigParser() 
        [parser.read(f) for f in glob.glob(os.path.join(path, "*.ini"))] 
        return dict(parser.items("vehicle")) if parser.has_section("vehicle") else {} 
 
    def setup_teleop_receiver(self): 
        teleop = json_collector(url="ipc:///byodr/teleop_c.sock", topic=b"aav/teleop/chatter", pop=True, event=self.quit_event, hwm=1) 
        teleop.start() 
        return teleop 
 
    def setup_publisher(self): 
        return JSONPublisher(url="ipc:///byodr/following.sock", topic="aav/following/controls") 
 
    def publish_command(self, throttle, steering, button_b=1, camera_pan=None): 
        cmd = {"throttle": throttle, "steering": steering, "button_b": button_b, "time": timestamp(), "navigator": {"route": None}} 
        camera_pan = 0
        if camera_pan is not None: 
            cmd["camera_pan"] = camera_pan 
        self.publisher.publish(cmd) 
        # self.logger.info(f"Sending command to teleop: {cmd}") 
 
    def safety_feature(self, boxes): 
        clear_path = self.clear_path 
        for box in boxes:                                     # Checking every detected person in the frame 
            x1, y1, x2, y2 = box.xyxy[0,:] 
            if (y2 - y1) >= UNSAFE_HEIGHT:                        # Detected person is too close to the robot if the height is too large 
                return 0 
        clear_path += 1                                       # Path is clear in this frame 
        return clear_path 
 
    def control_logic(self, results): 
        for r in results:                                     # Running the loop for each frame of the stream 
            boxes = r.boxes.cpu().numpy()                    # List of bounding boxes in the frame 
            self.clear_path = self.safety_feature(boxes)      # Checking for obstructions 
            throttle, steering = self.decide_control(boxes)   # Calculating control commands based on the results of image detection 
            request = self.teleop.get()                       # Checking for request to stop following 
            self.track_and_save_image(r) 
            try: 
                if request['following'] == "Stop Following":  # Sending no movement if following stopped 
                    self.logger.info("Stopping Following") 
                    self.current_throttle = 0 
                    self.current_steering = 0 
                    self.publish_command(self.current_throttle, self.current_steering) 
                    return 
            except: 
                pass 
            self.publish_command(throttle, steering)          # Sending calculated control commands 
 
    def smooth_controls(self, target_throttle, target_steering): 
        if self.current_throttle <= (target_throttle - SMOOTH_CONTROL_STEP):                # Smoothing only if the difference is greater than the control step 
            self.current_throttle += SMOOTH_CONTROL_STEP 
        else: 
            self.current_throttle = target_throttle                                         # Passing without smoothing if the difference is too small 
        # if abs(self.current_steering) <= abs(target_steering) - SMOOTH_CONTROL_STEP:        # Steering can be negative or positive 
        #     self.current_steering += math.copysign(SMOOTH_CONTROL_STEP, target_steering)    # Making sure steering has the correct sign 
        # else: 
        #     self.current_steering = target_steering 
        self.current_steering = target_steering # steering is not being smoothed 
 
 
    def decide_control(self, boxes): 
        if not boxes.xyxy.size:                                                             # No people detected in the frame 
            self.no_human_counter += 1 
            self.logger.info(f"No person detected for: {self.no_human_counter} frames") 
            if self.no_human_counter >= MAX_HUMAN_ABSENCE_FRAMES: 
                self.current_throttle = 0 
                self.current_steering = 0 
                return self.current_throttle, self.current_steering 
            else: 
                try: 
                    return self.current_throttle, self.current_steering                                                   # Passing the previous control command if it exists 
                except: 
                    return 0, 0 
 
        throttle, steering = 0, 0                   # No movement by default 
        self.no_human_counter = 0                   # Resetting the counter if a person is detected 
        for box in boxes:                           # Checking every detected person in the frame 
            try: 
                if box.id == boxes.id[0]:           # Get the first ID if it was assigned 
                    x1, y1, x2, y2 = box.xyxy[0,:]  # Coordinates of the top left and bottom right corners of bbox 
                    box_center = (x1 + x2) / 2 
                    box_height = y2 - y1 
                    box_width = x2 - x1 
                    # self.logger.info(f"Box center: {int(box_center)}, Box height: {int(box_height)}, Height/Width: {(box_height/box_width)}") 
            except: 
                if (box.xyxy == boxes.xyxy[0]).all: # Get the first result on the list if ID was not assigned (first result has most confidence) 
                    x1, y1, x2, y2 = box.xyxy[0,:] 
                    box_center = (x1 + x2) / 2 
                    box_height = y2 - y1 
                    box_width = x2 - x1 
                    # self.logger.info(f"Box center: {int(box_center)}, Box height: {int(box_height)}, Height/Width: {(box_height/box_width)}") 
             
            # if (box_height / box_width) >= 12:   # Person might be behind an obstruction if the bbox is too thin 
            #     self.clear_path = 0             # Resetting the amount of frames with clear path 
             
            # Smaller bbox height means the detected person is further away from the camera 
            if box_height <= START_HEIGHT:                                  # Starting movement if person is far enough 
                throttle = max(0.2, min(1, ((-(0.01) * box_height) + 3.6))) # 0.2 at 320p height; 1 at 240p height 
 
            # Keeping the user in the center of the camera view 
            if box_center < (SCREEN_CENTER - CENTER_OFFSET):      # left = negative steering 
                steering = min(0, max(-0.1, (0.00125 * box_center - 0.4)))  # 0 at 320p; 0.1 at 240p 
                if box_center < (SCREEN_CENTER - CENTER_OFFSET - SECOND_OFFSET): 
                    steering = min(-0.1, max(-0.5, (0.004375 * box_center - 1.15))) # 0.1 at 400p; 0.8 at 560 (forcing max 0.5)
                # steering = steering*(1.2-throttle)                        # Max steering is scaled down proportionally to the throttle value, max steering = 0.16 at throttle = 1 
                steering = steering*(1.15-throttle*0.75)                    # Max steering is scaled down proportionally to the throttle value, max steering = 0.32 at throttle = 1 
                if throttle == 0:                                           # Turning in place 
                    throttle = (abs(steering)/1.15)                     # Scaling back 
                    steering = -1                                           # Max steering, turning speed determined by throttle 
           
            elif box_center > (SCREEN_CENTER + CENTER_OFFSET):    # right = positive steering                 
                steering = max(0, min(0.1, (0.00125 * box_center - 0.4)))   # 0 at 320p; 0.1 at 400p 
                if box_center > (SCREEN_CENTER + CENTER_OFFSET + SECOND_OFFSET): 
                    steering = max(0.1, min(0.50, (0.004375 * box_center - 1.65)))  # 0.1 at 400p; 0.8 at 560 (forcing max 0.5)
                # steering = steering*(1.2-throttle)                    # Max steering is scaled down proportionally to the throttle value, max steering = 0.16 at throttle = 1 
                steering = steering*(1.15-throttle*0.75)                # Max steering is scaled down proportionally to the throttle value, max steering = 0.32 at throttle = 1 
                if throttle == 0: 
                    throttle = (steering/1.15) 
                    steering = 1 
             
            if abs(box_center - SCREEN_CENTER) >= (SCREEN_CENTER - THIRD_OFFSET) and box_height <= START_HEIGHT: 
                self.logger.info("Operator on the far side") 
                steering = steering/(1.15-throttle*0.75) 
 
        if self.clear_path <= MIN_CLEAR_PATH_FRAMES: 
            self.logger.info(f"Path obstructed") # No movement if too few frames with clear path passed or too many frames without any detection 
            throttle = 0 
        self.smooth_controls(throttle, steering)                        # Smoothing the movement if the commands are immediately large 
        return self.current_throttle, self.current_steering 
 
    def run(self): 
        self.publish_command(self.current_throttle, self.current_steering)  # Initializing with safe values 
        self.logger.info("Following ready to start") 
        errors = [] 
        _config = self.config 
        stream_uri = parse_option('ras.master.uri', str, '192.168.1.32', errors, **_config) 
        stream_uri = f"rtsp://user1:HaikuPlot876@{stream_uri[:-2]}64:554/Streaming/Channels/103" # Setting dynamic URI of the stream 
        while True: 
            request = self.teleop.get()                         # Checking for requests to start following 
            try: 
                if request['following'] == "Start Following":
                    self.reset_tracking_session() 
                    self.logger.info("Loading Yolov8 model") 
                    results = self.model.track(source=stream_uri, classes=0, stream=True, conf=0.4, persist=True, verbose=True) # Image recognition with assigning IDs to objects 
                    self.control_logic(results)                 # Calculating the control commands based on the model results 
            except: 
                pass 
                 
 
if __name__ == "__main__": 
    controller = FollowingController("yolov8n.engine") 
    controller.run()