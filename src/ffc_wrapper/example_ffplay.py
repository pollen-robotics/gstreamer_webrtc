import argparse
import os
import signal
import subprocess as sp
from typing import Dict, List

from utils import add_common_args

from ffc_wrapper import FFCWrapper


def spawn_procs(names: List[str]) -> Dict[str, sp.Popen[bytes]]:
    width, height = 1280, 720
    command = [
        "ffplay",
        "-i",
        "-",
        "-x",
        str(width),
        "-y",
        str(height),
        "-framerate",
        "60",
        "-fflags",
        "nobuffer",
        "-flags",
        "low_delay",
        "-framedrop",
        "-strict",
        "experimental",
    ]

    procs = {}
    try:
        for name in names:
            procs[name] = sp.Popen(command, stdin=sp.PIPE)  # Start the ffplay process
    except Exception:
        exit("Error: cannot run ffplay!\nTry running: sudo apt install ffmpeg")

    return procs


def main() -> None:
    parser = argparse.ArgumentParser()
    add_common_args(parser)

    args = parser.parse_args()

    ffcw = FFCWrapper(
        args.config,
        rescale=args.rescale,
        fps=args.fps,
        hardware_rectify=False,
        hardware_sync=True,
        usb2=args.force_usb2,
    )

    cams_names = ["left", "right"]

    procs = spawn_procs(cams_names)

    running = True

    while running:
        data, latency, ts = ffcw.get_data()

        # print("sync:", abs(ts[0] - ts[1]), "\n")
        for name in data.keys():
            procs[name].stdin.write(data[name])  # type: ignore[union-attr]

    for k in procs.keys():
        procs[k].stdin.close()  # type: ignore[union-attr]
        os.killpg(os.getpgid(procs[k].pid), signal.SIGTERM)


if __name__ == "__main__":
    main()
