import argparse
import logging
import os

from gst_signalling.aiortc_adapter import add_signaling_arguments

from ffc_wrapper.ffc_wrapper import FFCWrapper
from ffc_wrapper.utils import add_common_args
from gstreamer.avpipeline import GstAVPipeline


def main() -> None:
    parser = argparse.ArgumentParser(description="webrtc gstreamer producer/consumer")
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="enable verbose mode"
    )
    parser.add_argument("--producer-name", type=str)
    add_signaling_arguments(parser)  # signalling args
    add_common_args(parser)

    args = parser.parse_args()

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)
        os.environ["GST_DEBUG"] = "2"

    ffcw = FFCWrapper(
        args.config,
        rescale=args.rescale,
        fps=args.fps,
        hardware_rectify=False,
        hardware_sync=True,
        usb2=args.force_usb2,
    )

    avpipeline = GstAVPipeline(lowlatencyaudio=False)
    avpipeline.make_pipeline()
    video_left = avpipeline.get_appsrc("left")
    video_right = avpipeline.get_appsrc("right")

    avpipeline.start()

    try:
        while True:
            data, latency, _ = ffcw.get_data()
            # print(str(latency) + " ms")
            avpipeline.push_frame(video_left, data["left"])
            avpipeline.push_frame(video_right, data["right"])

    except KeyboardInterrupt:
        logging.info("User exit")
    finally:
        avpipeline.stop()


if __name__ == "__main__":
    main()
