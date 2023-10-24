import logging

import gi
import numpy as np

gi.require_version("Gst", "1.0")
import numpy.typing as npt
from gi.repository import Gst

# note about mypy: PyGObject not natively supported. errors explicitely ignored.


class GstAVPipeline:
    def __init__(self, lowlatencyaudio: bool = True):
        """Initialize GStreamer WebRTC app."""

        self._logger = logging.getLogger(__name__)
        self._pipeline = None
        self._appsrc_left = None
        self._appsrc_right = None
        self._lowlatencyaudio = lowlatencyaudio

        # Initialisation
        Gst.init(None)

    def get_appsrc(self, name: str):  # type: ignore[no-untyped-def]
        if name == "left":
            print(type(self._appsrc_left))
            return self._appsrc_left
        elif name == "right":
            return self._appsrc_right
        else:
            self._logger.warning(
                "Unknow appsrc name : f{name}. Should be left or right."
            )
            return None

    def _add_webrtcink(self):  # type: ignore[no-untyped-def]
        assert self._pipeline is not None
        webrtcsink = Gst.ElementFactory.make("webrtcsink")
        meta_structure = Gst.Structure.new_empty("meta")
        meta_structure.set_value("name", "robot")
        webrtcsink.set_property("meta", meta_structure)
        webrtcsink.set_property("congestion-control", "disabled")
        self._pipeline.add(webrtcsink)
        return webrtcsink

    def _add_appsrc(self, name: str):  # type: ignore[no-untyped-def]
        assert self._pipeline is not None
        appsrc = Gst.ElementFactory.make("appsrc")
        appsrc.set_property("name", name)
        appsrc.set_property("format", Gst.Format.TIME)
        appsrc.set_property("is-live", True)
        appsrc.set_property("min-latency", 30000)  # luxonis reports à 30ms latency
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

    def _add_alsasrc(self, lowlatencydevice: bool = True):  # type: ignore[no-untyped-def]
        assert self._pipeline is not None
        alsasrc = Gst.ElementFactory.make("alsasrc")
        if lowlatencydevice:
            alsasrc.set_property("device", "lowlatencysrc")
        alsasrc.set_property("buffer-time", 30000)
        alsasrc.set_property("latency-time", 10000)
        self._pipeline.add(alsasrc)
        return alsasrc

    def _add_opus_enc(self):  # type: ignore[no-untyped-def]
        assert self._pipeline is not None
        opusenc = Gst.ElementFactory.make("opusenc")
        opusenc.set_property("audio-type", "voice")
        self._pipeline.add(opusenc)

        audio_caps = Gst.caps_from_string("audio/x-opus")
        audio_caps.set_value("channels", 2)
        audio_caps.set_value("rate", 48000)
        audio_caps_capsfilter = Gst.ElementFactory.make("capsfilter")
        audio_caps_capsfilter.set_property("caps", audio_caps)
        self._pipeline.add(audio_caps_capsfilter)

        return opusenc, audio_caps_capsfilter

    def _set_stereo_video(self, webrtcsink) -> None:  # type: ignore[no-untyped-def]
        assert self._pipeline is not None
        self._logger.info("Set up stereo video pipeline")
        self._appsrc_left = self._add_appsrc("src_left")
        self._appsrc_right = self._add_appsrc("src_right")
        queue_left = self._add_queue()
        h264parse_left = self._add_h264parse()
        queue_right = self._add_queue()
        h264parse_right = self._add_h264parse()

        if not Gst.Element.link(self._appsrc_left, queue_left):
            self._logger.error("Failed to link appsrc -> queue")
        if not Gst.Element.link(queue_left, h264parse_left):
            self._logger.error("Failed to link queue -> h264parse")
        if not Gst.Element.link(h264parse_left, webrtcsink):
            self._logger.error("Failed to link h264parse -> webrtcsink")

        if not Gst.Element.link(self._appsrc_right, queue_right):
            self._logger.error("Failed to link appsrc -> queue")
        if not Gst.Element.link(queue_right, h264parse_right):
            self._logger.error("Failed to link queue -> h264parse")
        if not Gst.Element.link(h264parse_right, webrtcsink):
            self._logger.error("Failed to link h264parse -> webrtcsink")

    def _set_stereo_audio(self, webrtcsink) -> None:  # type: ignore[no-untyped-def]
        assert self._pipeline is not None
        self._logger.info("Set up stereo audio pipeline")
        alsasrc = self._add_alsasrc(self._lowlatencyaudio)
        queue_audio = self._add_queue()
        opusenc, audio_caps = self._add_opus_enc()

        if not Gst.Element.link(alsasrc, queue_audio):
            self._logger.error("Failed to link alsasrc -> queue")
        if not Gst.Element.link(queue_audio, opusenc):
            self._logger.error("Failed to link queue -> opusenc")
        if not Gst.Element.link(opusenc, audio_caps):
            self._logger.error("Failed to link opusenc -> caps")
        if not Gst.Element.link(audio_caps, webrtcsink):
            self._logger.error("Failed to link caps -> webrtcsink")

    def make_pipeline(self) -> None:
        self._pipeline = Gst.Pipeline.new()
        webrtcsink = self._add_webrtcink()  # type: ignore[no-untyped-call]

        self._set_stereo_video(webrtcsink)
        self._set_stereo_audio(webrtcsink)

    def push_frame(self, appsrc, data: npt.NDArray[np.uint8]) -> None:  # type: ignore[no-untyped-def]
        buf = Gst.Buffer.new_wrapped(data.tobytes())
        appsrc.emit("push-buffer", buf)

    def start(self) -> None:
        if self._pipeline is not None:
            ret = self._pipeline.set_state(Gst.State.PLAYING)
            if ret not in [Gst.StateChangeReturn.SUCCESS, Gst.StateChangeReturn.ASYNC]:
                self._logger.error(f"Failed to transition pipeline to PLAYING: {ret}")
        else:
            self._logger.warning("Pipeline not created. Nothing to do.")

    def stop(self) -> None:
        if self._pipeline is not None:
            self._pipeline.set_state(Gst.State.NULL)
            self._logger.info("Pipeline stopped")
