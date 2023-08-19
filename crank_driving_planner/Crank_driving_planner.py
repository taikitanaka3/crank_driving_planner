import time
import rclpy
import numpy as np
from rclpy.node import Node
from autoware_auto_planning_msgs.msg import Trajectory, Path
from geometry_msgs.msg import AccelWithCovarianceStamped, Point
from autoware_auto_perception_msgs.msg import PredictedObjects
from autoware_auto_vehicle_msgs.msg import VelocityReport
from nav_msgs.msg import Odometry

from .trajectory_uitl import *
from .predicted_objects_info import PredictedObjectsInfo

from .config import Config
from .dynamic_window_approach import DynamicWindowApproach

from .debug_plot import PlotMarker

class CrankDrigingPlanner(Node):
    def __init__(self):
        super().__init__('CrankDrigingPlanner')
        self.get_logger().info("Start CrankDrigingPlanner")
        
        ## Reference trajectory subscriber. Remap "/planning/scenario_planning/lane_driving/behavior_planning/path" ##
        self.create_subscription(Path ,"~/input/path", self.onTrigger, 10)

        ## Accrel subscriber ##
        self.create_subscription(AccelWithCovarianceStamped, "~/input/acceleration", self.onAcceleration, 10)

        ## Vehicle odometry subscriber ##
        self.create_subscription(Odometry, "~/input/odometry", self.onOdometry, 10)

        ## Vehicle odometry subscriber ##
        self.create_subscription(Odometry, "~/input/odometry", self.onOdometry, 10)

        # Predicted objects subscriber
        self.create_subscription(PredictedObjects, "~/input/perception", self.onPerception, 10)

        # Path publisher. Remap "/planning/scenario_planning/lane_driving/path" ##
        self.pub_path_ = self.create_publisher(Path, 
                                               "~/output/path", 
                                                10)
        # trajectory publisher. Remap "/planning/scenario_planning/lane_driving/trajectory" ##
        #self.pub_traj_ = self.create_publisher(Trajectory, "/planning/scenario_planning/lane_driving/trajectory", 10)
        self.pub_traj_ = self.create_publisher(Trajectory, "~/output/trajectory", 10)

        # Initialize input ##
        self.reference_path = None
        self.current_accel = None
        self.current_odometry = None
        self.crrent_longitudinal_velocity = 0.0
        self.ego_pose = None
        self.dynamic_objects = None
        self.left_bound  = None
        self.right_bound  = None

        self.vehicle_state = "drive"
        self.nano_seconds = 1000**3
        self.before_exec_time = -9999 * self.nano_seconds
        self.duration = 10.0

        self.stop_time = 0.0
        self.stop_duration = 30

        self.current_path_index =None
        self.path_search_range = 3
        self.change_next_path = 3.0 #[m]

        self.animation_flag = True
        self.debug = False
        
        if self.animation_flag:
            self.plot_marker = PlotMarker()


    ## Check if input data is initialized. ##
    def isReady(self):
        if self.reference_path is None:
            self.get_logger().warning("The reference path data has not ready yet.")
            return False
        if self.current_accel is None:
            self.get_logger().warning("The accel data has not ready yet.")
            return False
        if self.current_odometry is None:
            self.get_logger().warning("The odometry data has not ready yet.")
            return False

        if self.ego_pose is None:
                self.get_logger().warning("The ego pose data has not ready yet.")
                return False
        return True

    ## Callback function for odometry subscriber ##
    def onOdometry(self, msg: Odometry):
        self.current_odometry = msg
        self.ego_pose = self.current_odometry.pose.pose
        self.crrent_vel_x = self.current_odometry.twist.twist.linear.x
        self.crrent_vel_y = self.current_odometry.twist.twist.linear.y
        #self.get_logger().info("odometry {}".format(self.current_odometry.pose))

        if self.vehicle_state == "planning":
            return 

        if self.crrent_vel_x > 0:
            self.vehicle_state = "drive"
            self.stop_time = 0.0
        else:
            self.stop_time += 0.1

        if self.stop_time > self.stop_duration:
            self.vehicle_state = "stop"

    ## Callback function for accrel subscriber ##
    def onAcceleration(self, msg: AccelWithCovarianceStamped):
        # return geometry_msgs/Accel 
        self.current_accel = accel = [msg.accel.accel.linear.x, msg.accel.accel.linear.y, msg.accel.accel.linear.z]

    ## Callback function for predicted objects ##
    def onPerception(self, msg: PredictedObjects):
        self.dynamic_objects = msg

    def _near_path_search(self, ego_pose, left_bound, right_bound):
        path_min_index = self.current_path_index
        path_max_index = self.current_path_index + 1 
        diff_left = np.linalg.norm(ego_pose[0:2] - left_bound[self.current_path_index + 1])
        diff_right = np.linalg.norm(ego_pose[0:2] - right_bound[self.current_path_index + 1])

        if diff_left < self.change_next_path or diff_right < self.change_next_path:
            path_min_index = self.current_path_index
            path_max_index = self.current_path_index + 1 
            self.current_path_index += 1

        return path_min_index, path_max_index

    def _get_nearest_path_idx(self, ego_pose, left_bound, right_bound):
        left_diff_x = left_bound[:, 0] - ego_pose[0]
        left_diff_y = left_bound[:, 1] - ego_pose[1]
        left_diff = np.hypot(left_diff_x, left_diff_y)
        right_diff_x = right_bound[:, 0] - ego_pose[0]
        right_diff_y = right_bound[:, 1] - ego_pose[1]
        right_diff = np.hypot(right_diff_x, right_diff_y)
        self.current_path_index = min(left_diff.argmin(), right_diff.argmin())

    ## Callback function for path subscriber ##
    def onTrigger(self, msg: Path):
        if self.debug:
            self.get_logger().info("Get path. Processing crank driving planner...")
            self.get_logger().info("Vehicle state is {}".format(self.vehicle_state))
        self.reference_path = msg
        obj_pose = None

        if self.dynamic_objects is not None:
            #self.get_logger().info("Objects num {}".format(len(self.dynamic_objects.objects)))
            obj_info = PredictedObjectsInfo (self.dynamic_objects.objects)
            obj_pose = obj_info.objects_rectangle

        if not self.isReady():
            self.get_logger().info("Not ready")
            self.pub_path_.publish(self.reference_path)
            return 

        ego_pose_array = ConvertPoint2List(self.ego_pose)

        ## Set left and right bound
        if (self.left_bound is None) or (self.right_bound is None):
            self.left_bound = ConvertPointSeq2Array(self.reference_path.left_bound)
            self.right_bound = ConvertPointSeq2Array(self.reference_path.right_bound)

        ## Initialize current path index
        if self.current_path_index is None:
            self._get_nearest_path_idx(ego_pose_array, self.left_bound, self.right_bound)

        ## Check current path index
        path_min_index, path_max_index = self._near_path_search(ego_pose_array, self.left_bound, self.right_bound)

        ## Visualize objects, vehicle and path on matplotlib
        if self.animation_flag:
            reference_path_array = ConvertPath2Array(self.reference_path)
            self.plot_marker.plot_status(ego_pose_array, 
                                        object_pose =obj_pose, 
                                        left_bound=self.left_bound,
                                        right_bound=self.right_bound,
                                        index_min=path_min_index, 
                                        index_max =path_max_index,
                                        path=reference_path_array,
                                        )
        
        ## If the vehicke is driving, not execute optimize. ##
        if self.vehicle_state == "drive":
            if self.debug:
                self.get_logger().info("Publish reference path")
            self.pub_path_.publish(self.reference_path)
            return

        elif self.vehicle_state == "planning":
            if self.debug:
                self.get_logger().info("Planning now")
            waite_time = (self.get_clock().now().nanoseconds - self.before_exec_time)/(self.nano_seconds)
            if waite_time < self.duration:
                self.get_logger().info("Remaining wait time {}".format(self.duration - waite_time))
                return 
            else:
                self.vehicle_state = "drive"
                self.stop_time = 0.0
                return
        #=======================
        #==== Optimize path ====
        #=======================
        new_path = self.optimize_path(self.reference_path, self.ego_pose)
        

    def optimize_path(self, reference_path, ego_pose):
        new_path = reference_path
        reference_path_array = ConvertPath2Array(new_path)
        ego_pose_array = ConvertPoint2List(ego_pose)
        dist = reference_path_array[: , 0:2] - ego_pose_array[0:2]
        nearest_idx = np.argmin(dist, axis=0)[0]

        ## If vehicle is not stopped, publish reference path ##
        points = new_path.points
        self.get_logger().info("Publish optimized path")
        self.get_logger().info("Points num {}".format(len(points)))
        self.get_logger().info("Nearest idx {}".format(nearest_idx))
        
        ## Generate trajectory
        output_traj = Trajectory()
        output_traj.points = convertPathToTrajectoryPoints(self.reference_path)
        output_traj.header = new_path.header

        offset = 0.2
        for idx in range(20):
            output_traj.points[nearest_idx - idx].pose.position.y -= offset
            offset += 0.25
            #print(np.linalg.norm(reference_path_array[idx, 0:2] - reference_path_array[idx - 1, 0:2]))
            
        ## Path Optimize ##
        self.before_exec_time = self.get_clock().now().nanoseconds
        self.vehicle_state = "planning"

        self.pub_traj_.publish(output_traj)
        #self.pub_path_.publish(new_path)
        #return new_path 

def main(args=None):
    print('Hi from CrankDrigingPlanner')
    rclpy.init(args=args)
    
    crank_drining_plan_class = CrankDrigingPlanner()
    rclpy.spin(crank_drining_plan_class)

    crank_drining_plan_class.destroy_node()
    rclpy.shutdown()

if __name__ == '__main__':
    main()