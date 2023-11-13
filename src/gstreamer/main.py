import argparse
import logging
import os
import time
from typing import Any, Dict, Tuple

from gst_signalling.aiortc_adapter import add_signaling_arguments

from ffc_wrapper.ffc_wrapper import FFCWrapper
from ffc_wrapper.utils import add_common_args
from gstreamer.avpipeline import GstAVPipeline
from gstreamer.signalling import get_producer_id


def parse_args() -> argparse.Namespace:
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
        help="set accoustic echo cancellation level",
    )
    parser.add_argument(
        "--stream",
        choices=["audio", "video", "audiovideo"],
        help="stream selection",
        default="audiovideo",
    )

    add_signaling_arguments(parser)  # signalling args
    add_common_args(parser)

    return parser.parse_args()


def configure_camera(args: argparse.Namespace) -> Tuple[FFCWrapper, Dict[str, int]]:
    ffcw: FFCWrapper = None
    latency: Dict[str, int] = {}
    if args.stream != "audio":
        ffcw = FFCWrapper(
            args.config,
            rescale="720p",
            fps=args.fps,
            hardware_rectify=args.disable_hard_rectify,
            hardware_sync=True,
            usb2=args.force_usb2,
        )

        if ffcw is not None:
            for _ in range(10):
                _, latency, _ = ffcw.get_data()

    return ffcw, latency


def configure_pipeline(args: argparse.Namespace, latency: Dict[str, int], peer_id: str) -> Tuple[GstAVPipeline, Any, Any]:
    avpipeline = GstAVPipeline(
        args.name,
        args.signaling_host,
        args.signaling_port,
        stream_type=args.stream,
        lowlatencyaudio=args.lowlatencyaudio,
        localnetwork=args.localnetwork,
        peer_audio_id=peer_id,
        congestion=args.net_congestion,
    )

    video_left = None
    video_right = None

    if args.stream != "audio":
        avpipeline.make_pipeline(latency["left"])
        video_left = avpipeline.get_appsrc("left")
        video_right = avpipeline.get_appsrc("right")
    else:
        avpipeline.make_pipeline()

    return avpipeline, video_left, video_right


def main() -> None:
    args = parse_args()

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)
        os.environ["GST_DEBUG"] = "2"

    # Todo: not here
    peer_id = ""
    if args.remote_producer_peer_name and args.remote_producer_peer_name != "aiortc-peer":  # to fix
        peer_id = get_producer_id(args.signaling_host, args.signaling_port, args.remote_producer_peer_name)

    ffcw, latency = configure_camera(args)

    avpipeline, video_left, video_right = configure_pipeline(args, latency, peer_id)

    avpipeline.start()

    try:
        while True:
            if ffcw:
                data, latency, _ = ffcw.get_data()
                # print(str(latency) + " ns")
                avpipeline.push_frame(video_left, data["left"], latency["left"])
                avpipeline.push_frame(video_right, data["right"], latency["right"])
            else:
                time.sleep(0.1)

    except KeyboardInterrupt:
        logging.info("User exit")
    finally:
        avpipeline.stop()


if __name__ == "__main__":
    main()
