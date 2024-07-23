import argparse
from typing import Optional

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst
from gst_signalling.utils import find_producer_peer_id_by_name


class GstConsumer:
    def __init__(
        self, signalling_host: str, signalling_port: int, peer_id: Optional[str] = None, peer_name: Optional[str] = None
    ) -> None:
        Gst.init(None)

        self.pipeline = Gst.Pipeline.new("webRTC-consumer")
        self.source = Gst.ElementFactory.make("webrtcsrc")

        if not self.pipeline:
            print("Pipeline could be created.")
            exit(-1)

        if not self.source:
            print(
                "webrtcsrc component could not be created. Please make sure that the plugin is installed \
                (see https://gitlab.freedesktop.org/gstreamer/gst-plugins-rs/-/tree/main/net/webrtc)"
            )
            exit(-1)

        self.pipeline.add(self.source)

        if peer_id is None:
            peer_id = find_producer_peer_id_by_name(signalling_host, signalling_port, peer_name)
            print(f"found peer id: {peer_id}")

        self.source.connect("pad-added", self.webrtcsrc_pad_added_cb)
        signaller = self.source.get_property("signaller")
        signaller.set_property("producer-peer-id", peer_id)
        signaller.set_property("uri", f"ws://{signalling_host}:{signalling_port}")

    def dump_latency(self) -> None:
        query = Gst.Query.new_latency()
        self.pipeline.query(query)
        print(f"Pipeline latency {query.parse_latency()}")

    def _configure_webrtcbin(self, webrtcsrc: Gst.Element) -> None:
        if isinstance(webrtcsrc, Gst.Bin):
            webrtcbin_name = "webrtcbin0"
            webrtcbin = webrtcsrc.get_by_name(webrtcbin_name)
            assert webrtcbin is not None
            # jitterbuffer has a default 200 ms buffer. Should be ok to lower this in localnetwork config
            webrtcbin.set_property("latency", 10)

    def webrtcsrc_pad_added_cb(self, webrtcsrc: Gst.Element, pad: Gst.Pad) -> None:
        self._configure_webrtcbin(webrtcsrc)
        if pad.get_name().startswith("video"):  # type: ignore[union-attr]
            videodepay = Gst.ElementFactory.make("rtph264depay")
            assert videodepay is not None
            queue = Gst.ElementFactory.make("queue")
            assert queue is not None
            h264parse = Gst.ElementFactory.make("h264parse")
            assert h264parse is not None
            decoder = Gst.ElementFactory.make("nvh264dec")
            assert decoder is not None
            videoconvert = Gst.ElementFactory.make("videoconvert")
            assert videoconvert is not None
            sink = Gst.ElementFactory.make("autovideosink")
            assert sink is not None

            self.pipeline.add(videodepay)
            self.pipeline.add(queue)
            self.pipeline.add(h264parse)
            self.pipeline.add(decoder)
            self.pipeline.add(videoconvert)
            self.pipeline.add(sink)
            videodepay.link(queue)
            queue.link(h264parse)
            h264parse.link(decoder)
            decoder.link(videoconvert)
            videoconvert.link(sink)
            pad.link(videodepay.get_static_pad("sink"))  # type: ignore[arg-type]

            videodepay.sync_state_with_parent()
            queue.sync_state_with_parent()
            h264parse.sync_state_with_parent()
            videoconvert.sync_state_with_parent()
            decoder.sync_state_with_parent()
            sink.sync_state_with_parent()
        elif pad.get_name().startswith("audio"):  # type: ignore[union-attr]
            audiodepay = Gst.ElementFactory.make("rtpopusdepay")
            assert audiodepay is not None
            queue = Gst.ElementFactory.make("queue")
            assert queue is not None
            opusparse = Gst.ElementFactory.make("opusparse")
            assert opusparse is not None
            opusdec = Gst.ElementFactory.make("opusdec")
            assert opusdec is not None
            audioconvert = Gst.ElementFactory.make("audioconvert")
            assert audioconvert is not None
            audioresample = Gst.ElementFactory.make("audioresample")
            assert audioresample is not None
            sink = Gst.ElementFactory.make("autoaudiosink")
            assert sink is not None

            self.pipeline.add(audiodepay)
            self.pipeline.add(queue)
            self.pipeline.add(opusparse)
            self.pipeline.add(opusdec)
            self.pipeline.add(audioconvert)
            self.pipeline.add(audioresample)
            self.pipeline.add(sink)
            audiodepay.link(queue)
            queue.link(opusparse)
            opusparse.link(opusdec)
            opusdec.link(audioconvert)
            audioconvert.link(audioresample)
            audioresample.link(sink)
            pad.link(audiodepay.get_static_pad("sink"))  # type: ignore[arg-type]

            audiodepay.sync_state_with_parent()
            queue.sync_state_with_parent()
            opusdec.sync_state_with_parent()
            opusparse.sync_state_with_parent()
            audioconvert.sync_state_with_parent()
            audioresample.sync_state_with_parent()
            sink.sync_state_with_parent()

        GLib.timeout_add_seconds(5, self.dump_latency)

    def __del__(self) -> None:
        Gst.deinit()

    def get_bus(self) -> Gst.Bus:
        return self.pipeline.get_bus()

    def play(self) -> None:
        # Start playing
        ret = self.pipeline.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            print("Error starting playback.")
            exit(-1)
        print("playing ... (ctrl+c to quit)")

    def stop(self) -> None:
        print("stopping")
        self.pipeline.send_event(Gst.Event.new_eos())
        self.pipeline.set_state(Gst.State.NULL)


def process_msg(bus: Gst.Bus, pipeline: Gst.Pipeline) -> bool:
    msg = bus.timed_pop_filtered(10 * Gst.MSECOND, Gst.MessageType.ANY)
    if msg:
        if msg.type == Gst.MessageType.ERROR:
            err, debug = msg.parse_error()
            print(f"Error: {err}, {debug}")
            return False
        elif msg.type == Gst.MessageType.EOS:
            print("End-Of-Stream reached.")
            return False
        elif msg.type == Gst.MessageType.LATENCY:
            if pipeline:
                try:
                    pipeline.recalculate_latency()
                except Exception as e:
                    print("failed to recalculate warning, exception: %s" % str(e))
        # else:
        #    print(f"Message: {msg.type}")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="webrtc gstreamer simple consumer")
    parser.add_argument("--signaling-host", default="127.0.0.1", help="Gstreamer signaling host")
    parser.add_argument("--signaling-port", default=8443, help="Gstreamer signaling port")
    parser.add_argument(
        "--remote-producer-peer-id",
        type=str,
        help="producer peer_id",
    )
    parser.add_argument(
        "--remote-producer-peer-name",
        type=str,
        help="producer name",
    )

    args = parser.parse_args()

    if args.remote_producer_peer_id is None and args.remote_producer_peer_name is None:
        exit("You must set either remote_producer_peer_id or remote_producer_peer_name")

    consumer = GstConsumer(
        args.signaling_host, args.signaling_port, args.remote_producer_peer_id, args.remote_producer_peer_name
    )
    consumer.play()

    # Wait until error or EOS
    bus = consumer.get_bus()
    try:
        while True:
            if not process_msg(bus, consumer.pipeline):
                break

    except KeyboardInterrupt:
        print("User exit")
    finally:
        # Free resources
        consumer.stop()


if __name__ == "__main__":
    main()
