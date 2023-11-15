import argparse

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst


class GstRecorder:
    def __init__(self, signalling_host: str, signalling_port: int, peer_id: str, filename: str) -> None:
        Gst.init(None)

        self.pipeline = Gst.Pipeline.new("webRTC-recorder")
        source = Gst.ElementFactory.make("webrtcsrc")
        self.mux = Gst.ElementFactory.make("mp4mux")
        filesink = Gst.ElementFactory.make("filesink")
        filesink.set_property("location", filename)

        if not self.pipeline or not source or not filesink or not self.mux:
            print("Not all elements could be created.")
            exit(-1)

        # Set up the pipeline
        self.pipeline.add(source)
        self.pipeline.add(self.mux)
        self.pipeline.add(filesink)
        self.mux.link(filesink)

        source.connect("pad-added", self.webrtcsrc_pad_added_cb)
        signaller = source.get_property("signaller")
        signaller.set_property("producer-peer-id", peer_id)
        signaller.set_property("uri", f"ws://{signalling_host}:{signalling_port}")

    def __del__(self) -> None:
        Gst.deinit()

    def get_bus(self):  # type: ignore[no-untyped-def]
        return self.pipeline.get_bus()

    def record(self) -> None:
        # Start playing
        ret = self.pipeline.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            print("Error starting playback.")
            exit(-1)

    def stop(self) -> None:
        print("stopping")
        self.pipeline.send_event(Gst.Event.new_eos())
        self.pipeline.set_state(Gst.State.NULL)

    def webrtcsrc_pad_added_cb(self, webrtcsrc, pad) -> None:  # type: ignore[no-untyped-def]
        if pad.get_name().startswith("video"):
            receiver = Gst.ElementFactory.make("rtph264depay")
            h264parser = Gst.ElementFactory.make("h264parse")
            self.pipeline.add(receiver)
            self.pipeline.add(h264parser)
            pad.link(receiver.get_static_pad("sink"))
            receiver.link(h264parser)
            h264parser.link(self.mux)
            receiver.sync_state_with_parent()
            h264parser.sync_state_with_parent()
            self.mux.sync_state_with_parent()


def process_msg(bus) -> bool:  # type: ignore[no-untyped-def]
    msg = bus.timed_pop_filtered(10 * Gst.MSECOND, Gst.MessageType.ANY)
    if msg:
        if msg.type == Gst.MessageType.ERROR:
            err, debug = msg.parse_error()
            print(f"Error: {err}, {debug}")
            return False
        elif msg.type == Gst.MessageType.EOS:
            print("End-Of-Stream reached.")
            return False
        # else:
        #    print(f"Message: {msg.type}")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="webrtc gstreamer simple recorder")
    parser.add_argument("--signaling-host", default="127.0.0.1", help="Gstreamer signaling host")
    parser.add_argument("--signaling-port", default=8443, help="Gstreamer signaling port")
    parser.add_argument(
        "--remote-producer-peer-id",
        type=str,
        help="producer peer_id",
        required=True,
    )
    parser.add_argument(
        "--output",
        type=str,
        help="mp4 file",
        required=True,
    )

    args = parser.parse_args()

    recorder = GstRecorder(args.signaling_host, args.signaling_port, args.remote_producer_peer_id, args.output)
    recorder.record()

    # Wait until error or EOS
    bus = recorder.get_bus()  # type: ignore[no-untyped-call]
    try:
        while True:
            if not process_msg(bus):
                break

    except KeyboardInterrupt:
        print("User exit")
    finally:
        # Free resources
        recorder.stop()


if __name__ == "__main__":
    main()
