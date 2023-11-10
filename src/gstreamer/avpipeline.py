import logging
import threading
import time

import gi
import numpy as np

gi.require_version("Gst", "1.0")
import numpy.typing as npt
from gi.repository import Gst

# note about mypy: PyGObject not natively supported. errors explicitely ignored.


class GstAVPipeline:
    def __init__(
        self,
        name: str,
        signalling_host: str,
        signalling_port: int,
        stream_type: str,
        lowlatencyaudio: bool = True,
        localnetwork: bool = False,
        peer_audio_id: str = "",
        congestion: bool = True,
        aec: str = "normal",
    ):
        """Initialize GStreamer WebRTC app."""

        self._logger = logging.getLogger(__name__)
        self._name = name
        self._stream_type = stream_type
        self._signalling_host = signalling_host
        self._signalling_port = signalling_port
        self._pipeline = None
        self._appsrc_left = None
        self._appsrc_right = None
        self._lowlatencyaudio = lowlatencyaudio
        self._localnetwork = localnetwork
        self._peer_audio_id = peer_audio_id
        self._congestion = congestion
        self._aec = aec

        self._thread_bus_calls = threading.Thread(target=self.handle_bus_calls)
        self._thread_running = False

        # kept for insering webrtcdsp in between
        self._alsasrc = None
        self._queue_audio = None

        Gst.init(None)

    def get_appsrc(self, name: str):  # type: ignore[no-untyped-def]
        if name == "left":
            return self._appsrc_left
        elif name == "right":
            return self._appsrc_right
        else:
            self._logger.warning("Unknow appsrc name : f{name}. Should be left or right.")
            return None

    def _add_webrtcink(self):  # type: ignore[no-untyped-def]
        assert self._pipeline is not None
        webrtcsink = Gst.ElementFactory.make("webrtcsink")
        meta_structure = Gst.Structure.new_empty("meta")
        meta_structure.set_value("name", self._name)
        webrtcsink.set_property("meta", meta_structure)
        if self._localnetwork:
            webrtcsink.set_property("stun-server", None)
        if not self._congestion:
            webrtcsink.set_property("congestion-control", "disabled")
        signaller = webrtcsink.get_property("signaller")
        signaller.set_property("uri", f"ws://{self._signalling_host}:{self._signalling_port}")
        self._pipeline.add(webrtcsink)
        return webrtcsink

    def _add_appsrc(self, name: str, cam_latency_ns: int):  # type: ignore[no-untyped-def]
        assert self._pipeline is not None
        appsrc = Gst.ElementFactory.make("appsrc")
        appsrc.set_property("name", name)
        appsrc.set_property("format", Gst.Format.TIME)
        appsrc.set_property("is-live", True)
        appsrc.set_property("leaky-type", 2)
        appsrc.set_property("min-latency", cam_latency_ns)
        appsrc.set_property("max-buffers", 100)
        self._logger.info(f"Video latency configured to {cam_latency_ns}")
        self._pipeline.add(appsrc)
        return appsrc

    def _add_h264parse(self):  # type: ignore[no-untyped-def]
        assert self._pipeline is not None
        h264parse = Gst.ElementFactory.make("h264parse")
        self._pipeline.add(h264parse)
        return h264parse

    def _add_queue(self):  # type: ignore[no-untyped-def]
        assert self._pipeline is not None
        queue = Gst.ElementFactory.make("queue")
        self._pipeline.add(queue)
        return queue

    def _add_audioconvert(self):  # type: ignore[no-untyped-def]
        assert self._pipeline is not None
        audioconvert = Gst.ElementFactory.make("audioconvert")
        self._pipeline.add(audioconvert)
        return audioconvert

    def _add_audioresample(self):  # type: ignore[no-untyped-def]
        assert self._pipeline is not None
        audioresample = Gst.ElementFactory.make("audioresample")
        self._pipeline.add(audioresample)
        return audioresample

    def _add_alsasrc(self, lowlatencydevice: bool = True):  # type: ignore[no-untyped-def]
        assert self._pipeline is not None
        alsasrc = Gst.ElementFactory.make("alsasrc")
        if lowlatencydevice:
            alsasrc.set_property("device", "lowlatencysrc")
        alsasrc.set_property("buffer-time", 30000)
        alsasrc.set_property("latency-time", 10000)
        self._pipeline.add(alsasrc)
        return alsasrc

    def _add_alsasink(self, lowlatencydevice: bool = True):  # type: ignore[no-untyped-def]
        assert self._pipeline is not None
        alsasink = Gst.ElementFactory.make("alsasink")
        if lowlatencydevice:
            alsasink.set_property("device", "lowlatencysink")
        alsasink.set_property("buffer-time", 30000)
        alsasink.set_property("latency-time", 10000)
        self._pipeline.add(alsasink)
        return alsasink

    def _add_opus_enc(self):  # type: ignore[no-untyped-def]
        assert self._pipeline is not None
        opusenc = Gst.ElementFactory.make("opusenc")
        opusenc.set_property("audio-type", "restricted-lowdelay")
        opusenc.set_property("frame-size", 10)
        self._pipeline.add(opusenc)

        audio_caps = Gst.caps_from_string("audio/x-opus")
        audio_caps.set_value("channels", 2)
        audio_caps.set_value("rate", 48000)
        audio_caps_capsfilter = Gst.ElementFactory.make("capsfilter")
        audio_caps_capsfilter.set_property("caps", audio_caps)
        self._pipeline.add(audio_caps_capsfilter)

        return opusenc, audio_caps_capsfilter

    def _configure_webrtcbin(self, webrtcsrc) -> None:  # type: ignore[no-untyped-def]
        if self._localnetwork:
            webrtcbin = webrtcsrc.get_by_name("webrtcbin0")
            # jitterbuffer has a default 200 ms buffer. Should be ok to lower this in localnetwork config
            webrtcbin.set_property("latency", 50)

    def _webrtcsrc_pad_added_cb(self, webrtcsrc, pad) -> None:  # type: ignore[no-untyped-def]
        if pad.get_name().startswith("audio"):
            webrtcechoprobe = self._add_webrtcechoprobe()  # type: ignore[no-untyped-call]
            queue_audio_playback = self._add_queue()  # type: ignore[no-untyped-call]
            audioconvert = self._add_audioconvert()  # type: ignore[no-untyped-call]
            audioresample = self._add_audioresample()  # type: ignore[no-untyped-call]
            alsasink = self._add_alsasink(self._lowlatencyaudio)

            self._configure_webrtcbin(webrtcsrc)

            if self._add_alsasrc is None or self._aec == "off":
                pad.link(queue_audio_playback.get_static_pad("sink"))
            else:
                pad.link(webrtcechoprobe.get_static_pad("sink"))
                webrtcechoprobe.sync_state_with_parent()

                if not Gst.Element.link(webrtcechoprobe, queue_audio_playback):
                    self._logger.error("Failed to link webrtcechoprobe -> queue")

            queue_audio_playback.sync_state_with_parent()

            if not Gst.Element.link(queue_audio_playback, audioconvert):
                self._logger.error("Failed to link queue -> audioconvert")
            audioconvert.sync_state_with_parent()
            if not Gst.Element.link(audioconvert, audioresample):
                self._logger.error("Failed to link audioconvert -> audioresample")
            audioresample.sync_state_with_parent()
            if not Gst.Element.link(audioresample, alsasink):
                self._logger.error("Failed to link audioresample -> alsasink")
            alsasink.sync_state_with_parent()

            if self._alsasrc is not None or self._queue_audio is not None:
                webrtcdsp = self._add_webrtcdsp()
                self._alsasrc.unlink(self._queue_audio)
                if not Gst.Element.link(self._alsasrc, webrtcdsp):
                    self._logger.error("Failed to link alsasrc -> webrtcechoprobe")
                if not Gst.Element.link(webrtcdsp, self._queue_audio):
                    self._logger.error("Failed to link webrtcechoprobe -> queue")
                webrtcdsp.sync_state_with_parent()
                self._queue_audio.sync_state_with_parent()

    def _add_webrtcsrc(self, peer_audio_id: str) -> None:
        assert self._pipeline is not None
        webrtcsrc = Gst.ElementFactory.make("webrtcsrc")

        if self._localnetwork:
            webrtcsrc.set_property("stun-server", None)

        signaller = webrtcsrc.get_property("signaller")
        signaller.set_property("producer-peer-id", peer_audio_id)
        signaller.set_property("uri", f"ws://{self._signalling_host}:{self._signalling_port}")

        webrtcsrc.connect("pad-added", self._webrtcsrc_pad_added_cb)
        self._pipeline.add(webrtcsrc)

    def _add_webrtcdsp(self):  # type: ignore[no-untyped-def]
        assert self._pipeline is not None
        webrtcdsp = Gst.ElementFactory.make("webrtcdsp")
        if self._aec == "strong":
            webrtcdsp.set_property("delay-agnostic", True)
            webrtcdsp.set_property("echo-suppression", 3)
        self._pipeline.add(webrtcdsp)
        return webrtcdsp

    def _add_webrtcechoprobe(self):  # type: ignore[no-untyped-def]
        assert self._pipeline is not None
        webrtcechoprobe = Gst.ElementFactory.make("webrtcechoprobe")
        self._pipeline.add(webrtcechoprobe)
        return webrtcechoprobe

    def _set_stereo_video(self, webrtcsink, cam_latency: int) -> None:  # type: ignore[no-untyped-def]
        assert self._pipeline is not None
        self._logger.info("Set up stereo video pipeline")
        self._appsrc_left = self._add_appsrc("src_left", cam_latency)
        self._appsrc_right = self._add_appsrc("src_right", cam_latency)
        h264parse_left = self._add_h264parse()
        h264parse_right = self._add_h264parse()

        if not Gst.Element.link(self._appsrc_left, h264parse_left):
            self._logger.error("Failed to link appsrc -> h264parse")
        if not Gst.Element.link(h264parse_left, webrtcsink):
            self._logger.error("Failed to link h264parse -> webrtcsink")

        if not Gst.Element.link(self._appsrc_right, h264parse_right):
            self._logger.error("Failed to link appsrc -> h264parse")
        if not Gst.Element.link(h264parse_right, webrtcsink):
            self._logger.error("Failed to link h264parse -> webrtcsink")

    def _set_stereo_audio(self, webrtcsink) -> None:  # type: ignore[no-untyped-def]
        assert self._pipeline is not None
        self._logger.info("Set up stereo audio pipeline")
        self._alsasrc = self._add_alsasrc(self._lowlatencyaudio)
        self._queue_audio = self._add_queue()
        opusenc, audio_caps = self._add_opus_enc()

        if not Gst.Element.link(self._alsasrc, self._queue_audio):
            self._logger.error("Failed to link alsasrc -> queue")
        if not Gst.Element.link(self._queue_audio, opusenc):
            self._logger.error("Failed to link queue -> opusenc")
        if not Gst.Element.link(opusenc, audio_caps):
            self._logger.error("Failed to link opusenc -> caps")
        if not Gst.Element.link(audio_caps, webrtcsink):
            self._logger.error("Failed to link caps -> webrtcsink")

    def _set_audio_playback(self) -> None:
        self._logger.info("Set up audio playback pipeline")
        self._add_webrtcsrc(self._peer_audio_id)

    def make_pipeline(self, cam_latency: int = 0) -> None:
        self._pipeline = Gst.Pipeline.new()
        webrtcsink = self._add_webrtcink()  # type: ignore[no-untyped-call]

        if self._stream_type == "video" or self._stream_type == "audiovideo":
            self._set_stereo_video(webrtcsink, cam_latency)
        if self._stream_type == "audio" or self._stream_type == "audiovideo":
            self._set_stereo_audio(webrtcsink)
        if self._peer_audio_id != "":
            self._set_audio_playback()

    def push_frame(self, appsrc, data: npt.NDArray[np.uint8], latency_ns: int = 0) -> None:  # type: ignore[no-untyped-def]
        clock = appsrc.get_clock()
        if clock is None:
            self._logger.warning("Pipeline is not playing")
            return

        basetime = appsrc.get_base_time()
        now = clock.get_time()
        if now < basetime:
            self._logger.warning("Bastime is not valid")
            return

        time = now - basetime
        if latency_ns > time:
            # This frame was captured before the pipeline was started
            # It could be a good time to request a keyframe
            self._logger.warning("Skipping early captured frame")
            return

        buf = Gst.Buffer.new_wrapped(data.tobytes())
        buf.pts = Gst.CLOCK_TIME_NONE
        buf.dts = time - latency_ns

        appsrc.emit("push-buffer", buf)

    def start(self) -> None:
        if self._pipeline is not None:
            ret = self._pipeline.set_state(Gst.State.PLAYING)
            if ret not in [Gst.StateChangeReturn.SUCCESS, Gst.StateChangeReturn.ASYNC]:
                self._logger.error(f"Failed to transition pipeline to PLAYING: {ret}")

            self._thread_bus_calls.start()
        else:
            self._logger.warning("Pipeline not created. Nothing to do.")

    def stop(self) -> None:
        if self._pipeline is not None:
            self._pipeline.set_state(Gst.State.NULL)
            self._logger.info("Pipeline stopped")
        if self._thread_bus_calls:
            self._thread_running = False
            self._thread_bus_calls.join()

    def bus_call(self, message) -> bool:  # type: ignore[no-untyped-def]
        t = message.type
        if t == Gst.MessageType.EOS:
            self._logger.error("End-of-stream\n")
            return False
        elif t == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            self._logger.error("Error: %s: %s\n" % (err, debug))
            return False
        elif t == Gst.MessageType.STATE_CHANGED:
            if isinstance(message.src, Gst.Pipeline):
                old_state, new_state, pending_state = message.parse_state_changed()
                self._logger.info(("Pipeline state changed from %s to %s." % (old_state.value_nick, new_state.value_nick)))
                if old_state.value_nick == "paused" and new_state.value_nick == "ready":
                    self._logger.info("stopping bus message loop")
                    return False
        elif t == Gst.MessageType.LATENCY:
            if self._pipeline:
                try:
                    self._pipeline.recalculate_latency()
                except Exception as e:
                    self._logger.warning("failed to recalculate warning, exception: %s" % str(e))

        return True

    def handle_bus_calls(self) -> None:
        self._logger.info("starting bus call loop")
        self._thread_running = True
        bus = None
        while self._thread_running:
            if self._pipeline is not None:
                bus = self._pipeline.get_bus()
            if bus is not None:
                while bus.have_pending():
                    msg = bus.pop()
                    if not self.bus_call(msg):
                        self._thread_running = False
            time.sleep(0.1)
