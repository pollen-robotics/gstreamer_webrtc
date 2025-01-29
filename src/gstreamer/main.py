import argparse
import asyncio
import logging
import os
from threading import Thread
from typing import List, Optional, Tuple

import gi

gi.require_version("Gst", "1.0")
import time

import rclpy
from gi.repository import Gst
from gst_signalling.utils import add_signaling_arguments
from pollen_vision.camera_wrappers.depthai.teleop import TeleopWrapper
from pollen_vision.camera_wrappers.depthai.utils import (
    get_config_file_path,
    get_config_files_names,
    get_connected_devices,
)
from rclpy.executors import MultiThreadedExecutor

from gstreamer.avpipeline import GstAVPipeline
from gstreamer.ros_publisher import ROSPublisher


def parse_args() -> argparse.Namespace:
    valid_configs = get_config_files_names()
    parser = argparse.ArgumentParser(description="webrtc gstreamer producer/consumer")
    parser.add_argument("-v", "--verbose", action="store_true", help="enable verbose mode")
    parser.add_argument("--localnetwork", action="store_true", help="local network mode No STUN SERVER")
    parser.add_argument(
        "--net-congestion",
        action="store_true",
        help="enable network congestion management",
    )
    parser.add_argument(
        "--lowlatencyaudio",
        action="store_true",
        help="Use low latency audio alsa device lowlatencysink/src",
    )
    parser.add_argument(
        "--aec-level",
        choices=["off", "normal", "strong"],
        default="normal",
        help="set accoustic echo cancellation level",
    )
    parser.add_argument(
        "--stream",
        choices=["audio", "video", "audiovideo"],
        help="stream selection",
        default="audiovideo",
    )
    parser.add_argument(
        "--remote-producer-name",
        type=str,
        help="name of the remote peer to get audio from",
    )
    parser.add_argument(
        "--config",
        type=str,
        required=True,
        choices=valid_configs,
        help=f"Configutation file name : {valid_configs}",
    )
    parser.add_argument(
        "--force-usb2",
        action="store_true",
        help="Force USB2 mode",
    )
    parser.add_argument(
        "--fps",
        type=int,
        default=60,
        help="Frames per second (default 60)",
    )
    parser.add_argument(
        "--exposure_time",
        type=int,
        help="Manual exposure time (must also set iso manually). If neither are set, auto parameters are used.",
    )
    parser.add_argument(
        "--iso",
        type=int,
        help="Manual iso (must also set exposure_time manually). If neither are set, auto parameters are used.",
    )
    parser.add_argument(
        "--disable-hard-rectify",
        action="store_false",
        help="Disable hardware rectification",
    )
    parser.add_argument("--ros", action="store_true", help="pusblish camera images to ROS")

    add_signaling_arguments(parser)  # signalling args

    return parser.parse_args()


def configure_camera(args: argparse.Namespace) -> TeleopWrapper:
    logging.info("Configuring cameras...")
    teleop_wrapper: TeleopWrapper = None
    if args.stream != "audio":
        devices = get_connected_devices()
        if len(devices.keys()) == 0:
            logging.error("There is no luxonis camera !")

        head_mx_id = ""
        for k, v in devices.items():
            if v != "other":
                head_mx_id = k
                logging.info(f"Camera mxid : {k}")

        if head_mx_id == "":
            logging.error("Teleop camera is not found !")

        exposure_params = None
        if args.exposure_time is not None and args.iso is not None:
            exposure_params = (args.exposure_time, args.iso)
        elif (args.exposure_time is None and args.iso is not None) or (args.exposure_time is not None and args.iso is None):
            logging.warning("iso and exposure time must be set. Using auto exposure.")
        teleop_wrapper = TeleopWrapper(
            get_config_file_path(args.config),
            fps=args.fps,
            rectify=args.disable_hard_rectify,
            force_usb2=args.force_usb2,
            exposure_params=exposure_params,
            mx_id=head_mx_id,
        )

    return teleop_wrapper


def compute_camera_latency(teleop_wrapper: TeleopWrapper) -> int:
    """Return the minimum latency in nanosecs for configuring gstreamer appsrc"""
    logging.info("Compute camera latency...")
    latencies: List[int] = []
    if teleop_wrapper is not None:
        for _ in range(30):  # sample of 30 frames. first latencies are usually not accurate
            _, latency, _ = teleop_wrapper.get_data_h264()
            latencies.append(latency["left"].microseconds * 1000)  # to ns

    # tip gstreamer: reduce latency of one frame since h264parse is adding one frame latency
    offset = (int)(1.0 / teleop_wrapper.cam_config.fps * 1_000_000_000)

    return min(latencies) - offset


def configure_pipeline(
    args: argparse.Namespace, latency_ns: int, peer_id: str, stop_event: asyncio.Event
) -> Tuple[GstAVPipeline, Optional[Gst.Element], Optional[Gst.Element]]:
    logging.info("Configuring gstreamer pipeline...")
    avpipeline = GstAVPipeline(
        args.name,
        args.signaling_host,
        args.signaling_port,
        stream_type=args.stream,
        stop_event=stop_event,
        lowlatencyaudio=args.lowlatencyaudio,
        localnetwork=args.localnetwork,
        peer_audio_name=peer_id,
        congestion=args.net_congestion,
        aec=args.aec_level,
    )

    video_left = None
    video_right = None

    if args.stream != "audio":
        avpipeline.make_pipeline(latency_ns)
        video_left = avpipeline.get_appsrc("left")
        video_right = avpipeline.get_appsrc("right")
    else:
        avpipeline.make_pipeline()

    return avpipeline, video_left, video_right


def thread_ros_fun(teleop_wrapper: TeleopWrapper, asyncio_loop: asyncio.AbstractEventLoop, stop_event: asyncio.Event) -> None:
    rclpy.init()
    executor = MultiThreadedExecutor()
    rospublisher_left_cam = ROSPublisher(teleop_wrapper.cam_config, "left", asyncio_loop, stop_event)
    rospublisher_right_cam = ROSPublisher(teleop_wrapper.cam_config, "right", asyncio_loop, stop_event)
    executor.add_node(rospublisher_left_cam)
    executor.add_node(rospublisher_right_cam)
    executor_thread = Thread(target=executor.spin, daemon=True)
    executor_thread.start()
    while not stop_event.is_set():
        data, latency, _ = teleop_wrapper.get_data_mjpeg()
        rospublisher_left_cam.publish_img(data["left_mjpeg"].tobytes(), latency["left_mjpeg"].microseconds * 1000)
        rospublisher_right_cam.publish_img(data["right_mjpeg"].tobytes(), latency["right_mjpeg"].microseconds * 1000)
        time.sleep(0.005) # requires export PYTHONOPTIMIZE=1 to avoid a drop of performance
    # executor.shutdown()


async def main_loop(args: argparse.Namespace) -> None:
    logging.info("Starting teleoperation")

    teleop_wrapper = configure_camera(args)
    latency_ns = 0
    if teleop_wrapper is not None:
        latency_ns = compute_camera_latency(teleop_wrapper)

    stop_event = asyncio.Event()

    avpipeline, video_left, video_right = configure_pipeline(args, latency_ns, args.remote_producer_name, stop_event)

    await avpipeline.start()

    if args.ros:
        thread_ros = Thread(target=thread_ros_fun, args=(teleop_wrapper, asyncio.get_event_loop(), stop_event), daemon=True)
        thread_ros.start()

    try:
        while not stop_event.is_set():
            if teleop_wrapper:
                data, latency, _ = teleop_wrapper.get_data_h264()
                # print(str(latency))
                avpipeline.push_frame(video_left, data["left"], latency["left"].microseconds * 1000)
                avpipeline.push_frame(video_right, data["right"], latency["right"].microseconds * 1000)
                # get_data is blocking. giving space to async methods
                await asyncio.sleep(0)
            else:
                # audio mode. work done in gstreamer threads
                await asyncio.sleep(1)

    except KeyboardInterrupt:
        logging.info("User exit")
    except RuntimeError as e:
        logging.error(f"Runtime error : {e}")
        logging.info("Luxonis camera may be unpplugged")
    finally:
        stop_event.set()
        await avpipeline.stop()
        await avpipeline.cleanup()

    logging.info("Closing teleoperation")


def main() -> None:
    args = parse_args()

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)
        os.environ["GST_DEBUG"] = "3"
    else:
        logging.basicConfig(level=logging.INFO)

    asyncio.run(main_loop(args))


if __name__ == "__main__":
    main()
