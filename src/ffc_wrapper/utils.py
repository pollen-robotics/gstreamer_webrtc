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
    argParser.add_argument(
        "--exposure_time",
        type=int,
        help="Manual exposure time (must also set iso manually). If neither are set, auto parameters are used.",
    )
    argParser.add_argument(
        "--iso",
        type=int,
        help="Manual iso (must also set exposure_time manually). If neither are set, auto parameters are used.",
    )
