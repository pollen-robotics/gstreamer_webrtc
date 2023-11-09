import argparse
import logging
import os
import time

from depthai_wrappers.teleop_wrapper import TeleopWrapper
from gst_signalling.aiortc_adapter import add_signaling_arguments

from gstreamer.avpipeline import GstAVPipeline
from gstreamer.signalling import get_producer_id


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="webrtc gstreamer producer/consumer")
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="enable verbose mode"
    )
    parser.add_argument(
        "--localnetwork", action="store_true", help="local network mode No STUN SERVER"
    )
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
    parser.add_argument(
        "--remote-producer-name",
        type=str,
        help="name of the remote peer to get audio from",
    )
    parser.add_argument(
        "--config",
        type=str,
        required=True,
        help="Path to the configuration file.",
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

    add_signaling_arguments(parser)  # signalling args

    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)
        os.environ["GST_DEBUG"] = "2"

    # Todo: not here
    peer_id = ""
    if args.remote_producer_name:
        peer_id = get_producer_id(
            args.signaling_host, args.signaling_port, args.remote_producer_name
        )

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
    avpipeline.make_pipeline()

    teleop_wrapper = None
    if args.stream != "audio":
        teleop_wrapper = TeleopWrapper(
            args.config,
            fps=args.fps,
            rectify=True,
            force_usb2=args.force_usb2,
        )

        video_left = avpipeline.get_appsrc("left")
        video_right = avpipeline.get_appsrc("right")

    avpipeline.start()

    try:
        while True:
            if teleop_wrapper:
                data, latency, _ = teleop_wrapper.get_data()
                # print(str(latency) + " ms")
                avpipeline.push_frame(video_left, data["left"])
                avpipeline.push_frame(video_right, data["right"])
            else:
                time.sleep(0.1)

    except KeyboardInterrupt:
        logging.info("User exit")
    finally:
        avpipeline.stop()


if __name__ == "__main__":
    main()
