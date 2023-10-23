import argparse


def add_common_args(argParser: argparse.ArgumentParser) -> None:
    argParser.add_argument(
        "--config",
        type=str,
        required=True,
        help="Path to the configuration file.",
    )

    argParser.add_argument(
        "--rescale",
        type=str,
        default="no",
        choices=["no", "720p"],
        help="Rescale the images (default no)",
    )

    argParser.add_argument(
        "--fps",
        type=int,
        default=60,
        help="Frames per second (default 60)",
    )
    argParser.add_argument(
        "--force-usb2",
        action="store_true",
        help="Force USB2 mode",
    )
