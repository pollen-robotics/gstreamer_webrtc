import argparse

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst
from gst_signalling.aiortc_adapter import add_signaling_arguments

decoder = None


def webrtcsrc_pad_added_cb(webrtcsrc, pad) -> None:  # type: ignore[no-untyped-def]
    webrtcbin = webrtcsrc.get_by_name("webrtcbin0")
    webrtcbin.set_property("latency", 20)
    global decoder
    if decoder is not None:
        pad.link(decoder.get_static_pad("sink"))
        decoder.sync_state_with_parent()


def main() -> None:
    parser = argparse.ArgumentParser(description="webrtc gstreamer simple consumer")
    add_signaling_arguments(parser)  # signalling args
    args = parser.parse_args()

    assert args.role == "consumer"
    assert args.remote_producer_peer_id is not None

    # Initialize GStreamer
    Gst.init(None)

    # Create the elements
    pipeline = Gst.Pipeline.new("webRTC-player")
    source = Gst.ElementFactory.make("webrtcsrc", "webrtc-source")
    global decoder
    decoder = Gst.ElementFactory.make("videoconvert", "decoder")
    sink = Gst.ElementFactory.make("autovideosink", "video-output")

    if not pipeline or not source or not decoder or not sink:
        print("Not all elements could be created.")
        exit(-1)

    # Set up the pipeline
    pipeline.add(source)
    pipeline.add(decoder)
    pipeline.add(sink)
    decoder.link(sink)

    # Configure WebRTC Source
    # Here you need to set the appropriate properties for the webrtcsrc element
    # This will vary depending on your WebRTC setup and requirements.
    # For example:
    # source.set_property("stun-server", "stun://stun.l.google.com:19302")
    source.connect("pad-added", webrtcsrc_pad_added_cb)
    signaller = source.get_property("signaller")
    signaller.set_property("producer-peer-id", args.remote_producer_peer_id)
    signaller.set_property("uri", f"ws://{args.signaling_host}:{args.signaling_port}")

    # Start playing
    ret = pipeline.set_state(Gst.State.PLAYING)
    if ret == Gst.StateChangeReturn.FAILURE:
        print("Error starting playback.")
        exit(-1)

    # Wait until error or EOS
    bus = pipeline.get_bus()
    try:
        while True:
            msg = bus.timed_pop_filtered(10 * Gst.MSECOND, Gst.MessageType.ANY)
            if msg:
                if msg.type == Gst.MessageType.ERROR:
                    err, debug = msg.parse_error()
                    print(f"Error: {err}, {debug}")
                    break
                elif msg.type == Gst.MessageType.EOS:
                    print("End-Of-Stream reached.")
                    break
                # else:
                #    print(f"Message: {msg.type}")
    except KeyboardInterrupt:
        print("User exit")
    finally:
        # Free resources
        pipeline.set_state(Gst.State.NULL)
        Gst.deinit()


if __name__ == "__main__":
    main()
