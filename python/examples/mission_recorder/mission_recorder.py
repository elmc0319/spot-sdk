# Copyright (c) 2020 Boston Dynamics, Inc.  All rights reserved.
#
# Downloading, reproducing, distributing or otherwise using the SDK Software
# is subject to the terms and conditions of the Boston Dynamics Software
# Development Kit License (20191101-BDSDK-SL).

"""
Interactive command-line mission recorder
"""

from __future__ import print_function

import curses
import logging
import os
import signal
import threading
import time

from bosdyn.api import world_object_pb2
from bosdyn.api.graph_nav import recording_pb2
from bosdyn.api.mission import nodes_pb2
import bosdyn.api.robot_state_pb2 as robot_state_proto
import bosdyn.api.spot.robot_command_pb2 as spot_command_pb2

import bosdyn.client
from bosdyn.client import create_standard_sdk, ResponseError, RpcError
from bosdyn.client.async_tasks import AsyncPeriodicQuery, AsyncTasks
import bosdyn.client.channel
from bosdyn.client.estop import EstopClient, EstopEndpoint, EstopKeepAlive
from bosdyn.client.graph_nav import GraphNavClient
from bosdyn.client.lease import LeaseClient, LeaseKeepAlive
from bosdyn.client.power import PowerClient
from bosdyn.client.robot_command import RobotCommandClient, RobotCommandBuilder
from bosdyn.client.recording import GraphNavRecordingServiceClient, NotLocalizedToEndError
from bosdyn.client.robot_state import RobotStateClient
from bosdyn.client.time_sync import TimeSyncError
import bosdyn.client.util
from bosdyn.util import duration_str, format_metric, secs_to_hms
from bosdyn.client.frame_helpers import ODOM_FRAME_NAME
from bosdyn.client.world_object import WorldObjectClient

LOGGER = logging.getLogger()

VELOCITY_BASE_SPEED = 0.5  # m/s
VELOCITY_BASE_ANGULAR = 0.8  # rad/sec
VELOCITY_CMD_DURATION = 0.6  # seconds
COMMAND_INPUT_RATE = 0.1



def _grpc_or_log(desc, thunk):
    try:
        return thunk()
    except (ResponseError, RpcError) as err:
        LOGGER.error("Failed %s: %s" % (desc, err))


class ExitCheck(object):
    """A class to help exiting a loop, also capturing SIGTERM to exit the loop."""

    def __init__(self):
        self._kill_now = False
        signal.signal(signal.SIGTERM, self._sigterm_handler)
        signal.signal(signal.SIGINT, self._sigterm_handler)

    def __enter__(self):
        return self

    def __exit__(self, _type, _value, _traceback):
        return False

    def _sigterm_handler(self, _signum, _frame):
        self._kill_now = True

    def request_exit(self):
        """Manually trigger an exit (rather than sigterm/sigint)."""
        self._kill_now = True

    @property
    def kill_now(self):
        """Return the status of the exit checker indicating if it should exit."""
        return self._kill_now


class CursesHandler(logging.Handler):
    """logging handler which puts messages into the curses interface"""

    def __init__(self, wasd_interface):
        super(CursesHandler, self).__init__()
        self._wasd_interface = wasd_interface

    def emit(self, record):
        msg = record.getMessage()
        msg = msg.replace('\n', ' ').replace('\r', '')
        self._wasd_interface.add_message('{:s} {:s}'.format(record.levelname, msg))


class AsyncMetrics(AsyncPeriodicQuery):
    """Grab robot metrics every few seconds."""

    def __init__(self, robot_state_client):
        super(AsyncMetrics, self).__init__("robot_metrics", robot_state_client, LOGGER,
                                           period_sec=5.0)

    def _start_query(self):
        return self._client.get_robot_metrics_async()


class AsyncRobotState(AsyncPeriodicQuery):
    """Grab robot state."""

    def __init__(self, robot_state_client):
        super(AsyncRobotState, self).__init__("robot_state", robot_state_client, LOGGER,
                                              period_sec=0.2)

    def _start_query(self):
        return self._client.get_robot_state_async()


class RecorderInterface(object):
    """A curses interface for recording robot missions."""

    def __init__(self, robot, download_filepath):
        self._robot = robot

        # Flag indicating whether mission is currently being recorded
        self._recording = False

        # Filepath for the location to put the downloaded graph and snapshots.
        self._download_filepath = download_filepath

        # List of visited waypoints in current mission
        self._waypoint_path = []

        # Current waypoint id
        self._waypoint_id = 'NONE'

        # Create clients -- do not use the for communication yet.
        self._lease_client = robot.ensure_client(LeaseClient.default_service_name)
        try:
            self._estop_client = self._robot.ensure_client(EstopClient.default_service_name)
            self._estop_endpoint = EstopEndpoint(self._estop_client, 'GNClient', 9.0)
        except:
            # Not the estop.
            self._estop_client = None
            self._estop_endpoint = None

        self._power_client = robot.ensure_client(PowerClient.default_service_name)
        self._robot_state_client = robot.ensure_client(RobotStateClient.default_service_name)
        self._robot_command_client = robot.ensure_client(RobotCommandClient.default_service_name)
        self._world_object_client = robot.ensure_client(WorldObjectClient.default_service_name)

        # Setup the recording service client.
        self._recording_client = self._robot.ensure_client(
            GraphNavRecordingServiceClient.default_service_name)

        # Setup the graph nav service client.
        self._graph_nav_client = robot.ensure_client(GraphNavClient.default_service_name)

        self._robot_metrics_task = AsyncMetrics(self._robot_state_client)
        self._robot_state_task = AsyncRobotState(self._robot_state_client)
        self._async_tasks = AsyncTasks([self._robot_metrics_task, self._robot_state_task])
        self._lock = threading.Lock()
        self._command_dictionary = {
            ord('\t'): self._quit_program,
            ord('T'): self._toggle_time_sync,
            ord(' '): self._toggle_estop,
            ord('r'): self._self_right,
            ord('P'): self._toggle_power,
            ord('v'): self._sit,
            ord('f'): self._stand,
            ord('w'): self._move_forward,
            ord('s'): self._move_backward,
            ord('a'): self._strafe_left,
            ord('d'): self._strafe_right,
            ord('q'): self._turn_left,
            ord('e'): self._turn_right,
            ord('m'): self._start_recording,
            ord('g'): self._generate_mission,
        }
        self._locked_messages = ['', '', '']  # string: displayed message for user
        self._estop_keepalive = None
        self._exit_check = None

        # Stuff that is set in start()
        self._robot_id = None
        self._lease = None

    def start(self):
        """Begin communication with the robot."""
        self._lease = self._lease_client.acquire()

        self._robot_id = self._robot.get_id()
        if self._estop_endpoint is not None:
            self._estop_endpoint.force_simple_setup(
            )  # Set this endpoint as the robot's sole estop.

    def shutdown(self):
        """Release control of robot as gracefully as posssible."""
        LOGGER.info("Shutting down RecorderInterface.")
        if self._estop_keepalive:
            _grpc_or_log("stopping estop", self._estop_keepalive.stop)
            self._estop_keepalive = None
        if self._lease:
            _grpc_or_log("returning lease", lambda: self._lease_client.return_lease(self._lease))
            self._lease = None

    def __del__(self):
        self.shutdown()

    def flush_and_estop_buffer(self, stdscr):
        """Manually flush the curses input buffer but trigger any estop requests (space)"""
        key = ''
        while key != -1:
            key = stdscr.getch()
            if key == ord(' '):
                self._toggle_estop()

    def add_message(self, msg_text):
        """Display the given message string to the user in the curses interface."""
        with self._lock:
            self._locked_messages = [msg_text] + self._locked_messages[:-1]

    def message(self, idx):
        """Grab one of the 3 last messages added."""
        with self._lock:
            return self._locked_messages[idx]

    @property
    def robot_state(self):
        """Get latest robot state proto."""
        return self._robot_state_task.proto

    @property
    def robot_metrics(self):
        """Get latest robot metrics proto."""
        return self._robot_metrics_task.proto

    def drive(self, stdscr):
        """User interface to control the robot via the passed-in curses screen interface object."""
        with LeaseKeepAlive(self._lease_client) as lease_keep_alive, \
             ExitCheck() as self._exit_check:
            curses_handler = CursesHandler(self)
            curses_handler.setLevel(logging.INFO)
            LOGGER.addHandler(curses_handler)

            stdscr.nodelay(True)  # Don't block for user input.
            stdscr.resize(26, 96)
            stdscr.refresh()

            # for debug
            curses.echo()

            try:
                while not self._exit_check.kill_now:
                    self._async_tasks.update()
                    self._drive_draw(stdscr, lease_keep_alive)

                    try:
                        cmd = stdscr.getch()
                        # Do not queue up commands on client
                        self.flush_and_estop_buffer(stdscr)
                        self._drive_cmd(cmd)
                        time.sleep(COMMAND_INPUT_RATE)
                    except Exception:
                        # On robot command fault, sit down safely before killing the program.
                        self._safe_power_off()
                        time.sleep(2.0)
                        self.shutdown()
                        raise

            finally:
                LOGGER.removeHandler(curses_handler)

    def _count_visible_fiducials(self):
        """Return number of fiducials visible to robot."""
        request_fiducials = [world_object_pb2.WORLD_OBJECT_APRILTAG]
        fiducial_objects = self._world_object_client.list_world_objects(
            object_type=request_fiducials).world_objects
        return len(fiducial_objects)

    def _drive_draw(self, stdscr, lease_keep_alive):
        """Draw the interface screen at each update."""
        stdscr.clear()  # clear screen
        stdscr.resize(26, 96)
        stdscr.addstr(0, 0, '{:20s} {}'.format(self._robot_id.nickname,
                                               self._robot_id.serial_number))
        stdscr.addstr(1, 0, self._lease_str(lease_keep_alive))
        stdscr.addstr(2, 0, self._battery_str())
        stdscr.addstr(3, 0, self._estop_str())
        stdscr.addstr(4, 0, self._power_state_str())
        stdscr.addstr(5, 0, self._time_sync_str())
        stdscr.addstr(6, 0, self._waypoint_str())
        stdscr.addstr(7, 0, self._fiducial_str())
        for i in range(3):
            stdscr.addstr(9 + i, 2, self.message(i))
        self._show_metrics(stdscr)
        stdscr.addstr(13, 0, "Commands: [TAB]: quit                               ")
        stdscr.addstr(14, 0, "          [T]: Time-sync, [SPACE]: Estop, [P]: Power")
        stdscr.addstr(15, 0, "          [v]: Sit, [f]: Stand, [r]: Self-right     ")
        stdscr.addstr(16, 0, "          [wasd]: Directional strafing              ")
        stdscr.addstr(17, 0, "          [qe]: Turning                             ")
        stdscr.addstr(18, 0, "          [m]: Start recording mission              ")
        stdscr.addstr(19, 0, "          [g]: Stop recording and generate mission  ")

        stdscr.refresh()

    def _drive_cmd(self, key):
        """Run user commands at each update."""
        try:
            cmd_function = self._command_dictionary[key]
            cmd_function()

        except KeyError:
            if key and key != -1 and key < 256:
                self.add_message("Unrecognized keyboard command: '{}'".format(chr(key)))

    def _try_grpc(self, desc, thunk):
        try:
            return thunk()
        except (ResponseError, RpcError) as err:
            self.add_message("Failed {}: {}".format(desc, err))
            return None

    def _quit_program(self):
        self._sit()
        self.shutdown()
        if self._exit_check is not None:
            self._exit_check.request_exit()

    def _toggle_time_sync(self):
        if self._robot.time_sync.stopped:
            self._robot.time_sync.start()
        else:
            self._robot.time_sync.stop()

    def _toggle_estop(self):
        """toggle estop on/off. Initial state is ON"""
        if self._estop_client is not None and self._estop_endpoint is not None:
            if not self._estop_keepalive:
                self._estop_keepalive = EstopKeepAlive(self._estop_endpoint)
            else:
                self._try_grpc("stopping estop", self._estop_keepalive.stop)
                self._estop_keepalive.shutdown()
                self._estop_keepalive = None

    def _start_robot_command(self, desc, command_proto, end_time_secs=None):

        def _start_command():
            self._robot_command_client.robot_command(lease=None, command=command_proto,
                                                     end_time_secs=end_time_secs)

        self._try_grpc(desc, _start_command)

    def _self_right(self):
        self._start_robot_command('self_right', RobotCommandBuilder.selfright_command())

    def _sit(self):
        self._start_robot_command('sit', RobotCommandBuilder.sit_command())

    def _stand(self):
        self._start_robot_command('stand', RobotCommandBuilder.stand_command())

    def _move_forward(self):
        self._velocity_cmd_helper('move_forward', v_x=VELOCITY_BASE_SPEED)

    def _move_backward(self):
        self._velocity_cmd_helper('move_backward', v_x=-VELOCITY_BASE_SPEED)

    def _strafe_left(self):
        self._velocity_cmd_helper('strafe_left', v_y=VELOCITY_BASE_SPEED)

    def _strafe_right(self):
        self._velocity_cmd_helper('strafe_right', v_y=-VELOCITY_BASE_SPEED)

    def _turn_left(self):
        self._velocity_cmd_helper('turn_left', v_rot=VELOCITY_BASE_ANGULAR)

    def _turn_right(self):
        self._velocity_cmd_helper('turn_right', v_rot=-VELOCITY_BASE_ANGULAR)

    def _velocity_cmd_helper(self, desc='', v_x=0.0, v_y=0.0, v_rot=0.0):
        self._start_robot_command(
            desc, RobotCommandBuilder.velocity_command(v_x=v_x, v_y=v_y, v_rot=v_rot),
            end_time_secs=time.time() + VELOCITY_CMD_DURATION)

    def _return_to_origin(self):
        self._start_robot_command(
            'fwd_and_rotate',
            RobotCommandBuilder.trajectory_command(
                goal_x=0.0, goal_y=0.0, goal_heading=0.0, frame_name=ODOM_FRAME_NAME, params=None,
                body_height=0.0, locomotion_hint=spot_command_pb2.HINT_SPEED_SELECT_TROT),
            end_time_secs=time.time() + 20)

    def _toggle_power(self):
        power_state = self._power_state()
        if power_state is None:
            self.add_message('Could not toggle power because power state is unknown')
            return

        if power_state == robot_state_proto.PowerState.STATE_OFF:
            self._try_grpc("powering-on", self._request_power_on)
        else:
            self._try_grpc("powering-off", self._safe_power_off)

    def _request_power_on(self):
        bosdyn.client.power.power_on(self._power_client)

    def _safe_power_off(self):
        self._start_robot_command('safe_power_off', RobotCommandBuilder.safe_power_off_command())

    def _show_metrics(self, stdscr):
        metrics = self.robot_metrics
        if not metrics:
            return
        for idx, metric in enumerate(metrics.metrics):
            stdscr.addstr(2 + idx, 50, format_metric(metric))

    def _power_state(self):
        state = self.robot_state
        if not state:
            return None
        return state.power_state.motor_power_state

    def _lease_str(self, lease_keep_alive):
        alive = 'RUNNING' if lease_keep_alive.is_alive() else 'STOPPED'
        lease = '{}:{}'.format(self._lease.lease_proto.resource, self._lease.lease_proto.sequence)
        return 'Lease {} THREAD:{}'.format(lease, alive)

    def _power_state_str(self):
        power_state = self._power_state()
        if power_state is None:
            return ''
        state_str = robot_state_proto.PowerState.MotorPowerState.Name(power_state)
        return 'Power: {}'.format(state_str[6:])  # get rid of STATE_ prefix

    def _estop_str(self):
        if not self._estop_client:
            thread_status = 'NOT ESTOP'
        else:
            thread_status = 'RUNNING' if self._estop_keepalive else 'STOPPED'
        estop_status = '??'
        state = self.robot_state
        if state:
            for estop_state in state.estop_states:
                if estop_state.type == estop_state.TYPE_SOFTWARE:
                    estop_status = estop_state.State.Name(estop_state.state)[6:]  # s/STATE_//
                    break
        return 'Estop {} (thread: {})'.format(estop_status, thread_status)

    def _time_sync_str(self):
        if not self._robot.time_sync:
            return 'Time sync: (none)'
        if self._robot.time_sync.stopped:
            status = 'STOPPED'
            exception = self._robot.time_sync.thread_exception
            if exception:
                status = '{} Exception: {}'.format(status, exception)
        else:
            status = 'RUNNING'
        try:
            skew = self._robot.time_sync.get_robot_clock_skew()
            if skew:
                skew_str = 'offset={}'.format(duration_str(skew))
            else:
                skew_str = "(Skew undetermined)"
        except (TimeSyncError, RpcError) as err:
            skew_str = '({})'.format(err)
        return 'Time sync: {} {}'.format(status, skew_str)

    def _waypoint_str(self):
        state = self._graph_nav_client.get_localization_state()
        try:
            self._waypoint_id = state.localization.waypoint_id
            if self._waypoint_id == '':
                self._waypoint_id = 'NONE'

        except:
            self._waypoint_id = 'ERROR'

        if self._recording and self._waypoint_id != 'NONE' and self._waypoint_id != 'ERROR':
            if (self._waypoint_path == []) or (self._waypoint_id != self._waypoint_path[-1]):
                self._waypoint_path += [self._waypoint_id]
            return 'Current waypoint: {} [ RECORDING ]'.format(self._waypoint_id)

        return 'Current waypoint: {}'.format(self._waypoint_id)

    def _fiducial_str(self):
        return 'Visible fiducials: {}'.format(str(self._count_visible_fiducials()))

    def _battery_str(self):
        if not self.robot_state:
            return ''
        battery_state = self.robot_state.battery_states[0]
        status = battery_state.Status.Name(battery_state.status)
        status = status[7:]  # get rid of STATUS_ prefix
        if battery_state.charge_percentage.value:
            bar_len = int(battery_state.charge_percentage.value) // 10
            bat_bar = '|{}{}|'.format('=' * bar_len, ' ' * (10 - bar_len))
        else:
            bat_bar = ''
        time_left = ''
        if battery_state.estimated_runtime:
            time_left = ' ({})'.format(secs_to_hms(battery_state.estimated_runtime.seconds))
        return 'Battery: {}{}{}'.format(status, bat_bar, time_left)

    def _fiducial_visible(self):
        """Return True if robot can see fiducial."""
        request_fiducials = [world_object_pb2.WORLD_OBJECT_APRILTAG]
        fiducial_objects = self._world_object_client.list_world_objects(
            object_type=request_fiducials).world_objects
        self.add_message("Fiducial objects: " + str(fiducial_objects))

        if len(fiducial_objects) > 0:
            return True

        return False

    def _start_recording(self):
        """Start recording a map."""
        if self._count_visible_fiducials() == 0:
            self.add_message("ERROR: Can't start recording -- No fiducials in view.")
            return

        self._waypoint_path = []
        status = self._recording_client.start_recording()
        if status != recording_pb2.StartRecordingResponse.STATUS_OK:
            self.add_message("Start recording failed.")
        else:
            self.add_message("Successfully started recording a map.")
            self._recording = True

    def _stop_recording(self):
        """Stop or pause recording a map."""
        if not self._recording:
            return True
        self._recording = False

        try:
            status = self._recording_client.stop_recording()
            if status != recording_pb2.StopRecordingResponse.STATUS_OK:
                self.add_message("Stop recording failed.")
                return False

            self.add_message("Successfully stopped recording a map.")
            return True

        except NotLocalizedToEndError:
            self.add_message("ERROR: Move to final waypoint before stopping recording.")
            return False

    def _generate_mission(self):
        """Save graph map and mission file."""

        # Check whether mission has been recorded
        if not self._recording:
            self.add_message("ERROR: No mission recorded.")
            return

        # Stop recording mission
        if not self._stop_recording():
            self.add_message("ERROR: Error while stopping recording.")
            return

        # Save graph map
        os.mkdir(self._download_filepath)
        self._download_full_graph()

        # Generate mission
        mission = self._make_mission()

        # Save mission file
        os.mkdir(self._download_filepath + "/missions")
        mission_filepath = self._download_filepath + "/missions/autogenerated"
        write_mission(mission, mission_filepath)

        # Quit program
        self._quit_program()

    def _download_full_graph(self):
        """Download the graph and snapshots from the robot."""
        graph = self._graph_nav_client.download_graph()
        if graph is None:
            self.add_message("Failed to download the graph.")
            return
        self._write_full_graph(graph)
        self.add_message("Graph downloaded with {} waypoints and {} edges".format(
            len(graph.waypoints), len(graph.edges)))
        # Download the waypoint and edge snapshots.
        self._download_and_write_waypoint_snapshots(graph.waypoints)
        self._download_and_write_edge_snapshots(graph.edges)

    def _write_full_graph(self, graph):
        """Download the graph from robot to the specified, local filepath location."""
        graph_bytes = graph.SerializeToString()
        write_bytes(self._download_filepath, '/graph', graph_bytes)

    def _download_and_write_waypoint_snapshots(self, waypoints):
        """Download the waypoint snapshots from robot to the specified, local filepath location."""
        num_waypoint_snapshots_downloaded = 0
        for waypoint in waypoints:
            try:
                waypoint_snapshot = self._graph_nav_client.download_waypoint_snapshot(
                    waypoint.snapshot_id)
            except Exception:
                # Failure in downloading waypoint snapshot. Continue to next snapshot.
                self.add_message("Failed to download waypoint snapshot: " + waypoint.snapshot_id)
                continue
            write_bytes(self._download_filepath + '/waypoint_snapshots', '/' + waypoint.snapshot_id,
                        waypoint_snapshot.SerializeToString())
            num_waypoint_snapshots_downloaded += 1
            self.add_message("Downloaded {} of the total {} waypoint snapshots.".format(
                num_waypoint_snapshots_downloaded, len(waypoints)))

    def _download_and_write_edge_snapshots(self, edges):
        """Download the edge snapshots from robot to the specified, local filepath location."""
        num_edge_snapshots_downloaded = 0
        for edge in edges:
            try:
                edge_snapshot = self._graph_nav_client.download_edge_snapshot(edge.snapshot_id)
            except Exception:
                # Failure in downloading edge snapshot. Continue to next snapshot.
                self.add_message("Failed to download edge snapshot: " + edge.snapshot_id)
                continue
            write_bytes(self._download_filepath + '/edge_snapshots', '/' + edge.snapshot_id,
                        edge_snapshot.SerializeToString())
            num_edge_snapshots_downloaded += 1
            self.add_message("Downloaded {} of the total {} edge snapshots.".format(
                num_edge_snapshots_downloaded, len(edges)))

    def _make_mission(self):
        """ Create a mission that visits each waypoint on stored path."""
        # Create a Sequence that visits all the waypoints.
        sequence = nodes_pb2.Sequence()
        sequence.children.add().CopyFrom(self._make_localize_node())

        for waypoint_id in self._waypoint_path:
            sequence.children.add().CopyFrom(self._make_goto_node(waypoint_id))

        # Return a Node with the Sequence.
        ret = nodes_pb2.Node()
        ret.name = "visit %d waypoints" % len(self._waypoint_path)
        ret.impl.Pack(sequence)
        return ret

    def _make_goto_node(self, waypoint_id):
        """ Create a leaf node that will go to the waypoint. """
        ret = nodes_pb2.Node()
        ret.name = "goto %s" % waypoint_id
        impl = nodes_pb2.BosdynNavigateTo()
        impl.destination_waypoint_id = waypoint_id
        ret.impl.Pack(impl)
        return ret

    def _make_localize_node(self):
        """Make localization node."""
        loc = nodes_pb2.Node()
        loc.name = "localize robot"
        impl = nodes_pb2.BosdynGraphNavLocalize()
        loc.impl.Pack(impl)
        return loc


def write_bytes(filepath, filename, data):
    """Write data to a file."""
    os.makedirs(filepath, exist_ok=True)
    with open(filepath + filename, 'wb+') as f:
        f.write(data)
        f.close()


def write_mission(mission, filename):
    """ Write a mission to disk. """
    open(filename, 'wb').write(mission.SerializeToString())


def setup_logging(verbose):
    """Log to file at debug level, and log to console at INFO or DEBUG (if verbose).

    Returns the stream/console logger so that it can be removed when in curses mode.
    """
    LOGGER.setLevel(logging.DEBUG)
    log_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')

    # Save log messages to file wasd.log for later debugging.
    file_handler = logging.FileHandler('wasd.log')
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(log_formatter)
    LOGGER.addHandler(file_handler)

    # The stream handler is useful before and after the application is in curses-mode.
    if verbose:
        stream_level = logging.DEBUG
    else:
        stream_level = logging.INFO

    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(stream_level)
    stream_handler.setFormatter(log_formatter)
    LOGGER.addHandler(stream_handler)
    return stream_handler


def main():
    """Command-line interface."""
    import argparse

    parser = argparse.ArgumentParser()
    bosdyn.client.util.add_common_arguments(parser)
    parser.add_argument('directory', help='Output directory for graph map and mission file.')
    
    options = parser.parse_args()

    stream_handler = setup_logging(options.verbose)

    # Create robot object.
    sdk = create_standard_sdk('MissionRecorderClient')
    sdk.load_app_token(options.app_token)
    robot = sdk.create_robot(options.hostname)
    try:
        robot.authenticate(options.username, options.password)
        robot.start_time_sync()
    except RpcError as err:
        LOGGER.error("Failed to communicate with robot: %s" % err)
        return False

    recorder_interface = RecorderInterface(robot, options.directory)
    try:
        recorder_interface.start()
    except (ResponseError, RpcError) as err:
        LOGGER.error("Failed to initialize robot communication: %s" % err)
        return False

    try:
        # Run recorder interface in curses mode, then restore terminal config.
        curses.wrapper(recorder_interface.drive)
    except Exception as e:
        LOGGER.error("Mission recorder has thrown an error: %s" % repr(e))
    finally:
        # Restore stream handler after curses mode.
        LOGGER.addHandler(stream_handler)

    return True


if __name__ == "__main__":
    if not main():
        os._exit(1)
    os._exit(0)