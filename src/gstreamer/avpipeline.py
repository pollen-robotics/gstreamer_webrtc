import logging

import gi
import numpy as np

# import threading


gi.require_version("Gst", "1.0")
# note about mypy: PyGObject not natively supported. errors explicitely ignored.
import asyncio

# import time
from typing import Any, Dict, List, Optional

import numpy.typing as npt
from gi.repository import GLib, Gst
from gst_signalling import GstSignallingListener


class GstAVPipeline:
    def __init__(
        self,
        name: str,
        signalling_host: str,
        signalling_port: int,
        stream_type: str,
        lowlatencyaudio: bool = True,
        localnetwork: bool = False,
        peer_audio_name: Optional[str] = None,
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
        self._peer_audio_name = peer_audio_name
        self._peer_audio_id = ""
        self._congestion = congestion
        self._aec = aec

        # self._thread_bus_calls = threading.Thread(target=self._handle_bus_calls, daemon=True)
        self._coro_bus_calls = None
        # self._bus_calls_task = None
        self._loop = GLib.MainLoop()

        # self._thread_remote_producer = threading.Thread(target=self.find_remote_producer, daemon=True)
        self._peer_audio_listener: Optional[GstSignallingListener] = None
        self._listener_task = None
        if self._peer_audio_name is not None:
            # self._thread_remote_producer.start()
            self._peer_audio_listener = GstSignallingListener(
                host=signalling_host,
                port=signalling_port,
                name=self._peer_audio_name,
            )
            self._peer_audio_listener.on("PeerStatusChanged", self._handle_peer_status_changed)
            self._listener_task = asyncio.create_task(self._peer_audio_listener.serve4ever())

        # self._mutex = threading.Lock()
        self._mutex = asyncio.Lock()

        # kept for insering webrtcdsp in between
        self._alsasrc = None
        self._queue_audio = None
        self._webrtcdsp = None
        self._components_webrtcsrc: List[Any] = []

        Gst.init(None)

    async def __del__(self) -> None:
        if self._listener_task:
            self._listener_task.cancel()
        if self._peer_audio_listener:
            await self._peer_audio_listener.close()
        Gst.deinit()

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
        webrtcsink.set_property("do-retransmission", False)  # lost packets will arrive too late anyway
        if self._localnetwork:
            webrtcsink.set_property("stun-server", None)
        if not self._congestion:
            webrtcsink.set_property("congestion-control", "disabled")
        self._signaller = webrtcsink.get_property("signaller")
        self._signaller.set_property("uri", f"ws://{self._signalling_host}:{self._signalling_port}")
        self._signaller.connect("producer-added", self._producer_added_cb)
        self._signaller.connect("producer-removed", self._producer_removed_cb)
        self._pipeline.add(webrtcsink)
        return webrtcsink

    def _producer_added_cb(self, producer_id, meta) -> None:  # type: ignore[no-untyped-def]
        self._logger.debug(" *** Producer added")
        self._logger.debug(f" {producer_id} {meta}")

    def _producer_removed_cb(self, obj, producer_id, meta) -> None:  # type: ignore[no-untyped-def]
        self._logger.debug(" *** Producer removed")
        self._logger.debug(f" {producer_id} {meta}")

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
        # alsasrc.set_property("buffer-time", 30000)
        # alsasrc.set_property("latency-time", 10000)
        self._pipeline.add(alsasrc)
        return alsasrc

    def _add_alsasink(self, lowlatencydevice: bool = True):  # type: ignore[no-untyped-def]
        assert self._pipeline is not None
        alsasink = Gst.ElementFactory.make("alsasink")
        if lowlatencydevice:
            alsasink.set_property("device", "lowlatencysink")
        # alsasink.set_property("buffer-time", 30000)
        # alsasink.set_property("latency-time", 10000)
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
            elements = []

            it = webrtcsrc.iterate_elements()
            while True:
                result, element = it.next()
                if result != Gst.IteratorResult.OK:
                    break
                elements.append(element)
                self._logger.debug(f"****{element.name}")

            webrtcbin = webrtcsrc.get_by_name("webrtcbin0")
            # jitterbuffer has a default 200 ms buffer. Should be ok to lower this in localnetwork config
            webrtcbin.set_property("latency", 50)

    def _webrtcsrc_pad_added_cb(self, webrtcsrc, pad) -> None:  # type: ignore[no-untyped-def]
        if pad.get_name().startswith("audio"):
            self._logger.info("Connecting audio client")
            webrtcechoprobe = self._add_webrtcechoprobe()  # type: ignore[no-untyped-call]
            queue_audio_playback = self._add_queue()  # type: ignore[no-untyped-call]
            audioconvert = self._add_audioconvert()  # type: ignore[no-untyped-call]
            audioresample = self._add_audioresample()  # type: ignore[no-untyped-call]
            alsasink = self._add_alsasink(self._lowlatencyaudio)

            self._components_webrtcsrc.append(webrtcechoprobe)
            self._components_webrtcsrc.append(queue_audio_playback)
            self._components_webrtcsrc.append(audioconvert)
            self._components_webrtcsrc.append(audioresample)
            self._components_webrtcsrc.append(alsasink)

            self._configure_webrtcbin(webrtcsrc)

            if self._aec == "off":
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

            if (self._alsasrc is not None or self._queue_audio is not None) and self._aec != "off":
                self._webrtcdsp = self._add_webrtcdsp()
                self._components_webrtcsrc.append(self._webrtcdsp)
                self._alsasrc.unlink(self._queue_audio)
                if not Gst.Element.link(self._alsasrc, self._webrtcdsp):
                    self._logger.error("Failed to link alsasrc -> webrtdsp")
                if not Gst.Element.link(self._webrtcdsp, self._queue_audio):
                    self._logger.error("Failed to link webrtcdsp -> queue")
                self._webrtcdsp.sync_state_with_parent()
                self._queue_audio.sync_state_with_parent()

            GLib.timeout_add_seconds(5, self.dump_latency)

    def _add_webrtcsrc(self, peer_audio_id: str) -> None:
        self._logger.debug("Add webrtc src")
        assert self._pipeline is not None
        webrtcsrc = Gst.ElementFactory.make("webrtcsrc")

        if self._localnetwork:
            webrtcsrc.set_property("stun-server", None)

        signaller = webrtcsrc.get_property("signaller")
        signaller.set_property("producer-peer-id", peer_audio_id)
        signaller.set_property("uri", f"ws://{self._signalling_host}:{self._signalling_port}")

        webrtcsrc.connect("pad-added", self._webrtcsrc_pad_added_cb)

        self._pipeline.add(webrtcsrc)
        self._components_webrtcsrc.append(webrtcsrc)

    def _add_webrtcdsp(self):  # type: ignore[no-untyped-def]
        assert self._pipeline is not None
        webrtcdsp = Gst.ElementFactory.make("webrtcdsp")
        if self._aec == "strong":
            webrtcdsp.set_property("delay-agnostic", True)
            webrtcdsp.set_property("echo-suppression-level", 2)
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

    def make_pipeline(self, cam_latency: int = 0) -> None:
        self._pipeline = Gst.Pipeline.new()
        webrtcsink = self._add_webrtcink()  # type: ignore[no-untyped-call]

        if self._stream_type == "video" or self._stream_type == "audiovideo":
            self._set_stereo_video(webrtcsink, cam_latency)
        if self._stream_type == "audio" or self._stream_type == "audiovideo":
            self._set_stereo_audio(webrtcsink)

    def push_frame(self, appsrc, data: npt.NDArray[np.uint8], latency_ns: int = 0) -> None:  # type: ignore[no-untyped-def]
        clock = appsrc.get_clock()
        if clock is None:
            self._logger.warning("Pipeline is not playing")
            return

        basetime = appsrc.get_base_time()
        now = clock.get_time()
        if now < basetime:
            self._logger.warning("Basetime is not valid")
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

    def dump_latency(self) -> None:
        if self._pipeline is not None:
            query = Gst.Query.new_latency()
            self._pipeline.query(query)
            self._logger.info(f"Pipeline latency {query.parse_latency()}")

    async def start(self) -> None:
        async with self._mutex:
            if self._pipeline is not None:
                ret = self._pipeline.set_state(Gst.State.PLAYING)
                if ret not in [Gst.StateChangeReturn.SUCCESS, Gst.StateChangeReturn.ASYNC]:
                    self._logger.error(f"Failed to transition pipeline to PLAYING: {ret}")

                """
                if not self._thread_bus_calls.is_alive():
                    self._thread_bus_calls.start()
                """
                if self._coro_bus_calls is None:
                    self._coro_bus_calls = asyncio.to_thread(self._handle_bus_calls)

                GLib.timeout_add_seconds(10, self.dump_latency)
            else:
                self._logger.warning("Pipeline not created. Nothing to do.")

    async def stop(self) -> None:
        async with self._mutex:
            if self._pipeline is not None:
                self._pipeline.set_state(Gst.State.NULL)
                self._logger.info("Pipeline stopped")
            """
            if self._thread_bus_calls:
                self._loop.quit()
                self._thread_bus_calls.join()
            """
            if self._coro_bus_calls:
                self._loop.quit()
                await self._coro_bus_calls
                self._coro_bus_calls = None
                # self._bus_calls_task.cancel()
        self._logger.info("Pipeline stopped")

    def bus_message_cb(self, bus, msg, loop) -> bool:  # type: ignore[no-untyped-def]
        t = msg.type
        if t == Gst.MessageType.EOS:
            self._logger.error("End-of-stream\n")
            return False
        elif t == Gst.MessageType.ERROR:
            err, debug = msg.parse_error()
            self._logger.error("Error: %s: %s\n" % (err, debug))
            loop.quit()
            return False
        elif t == Gst.MessageType.STATE_CHANGED:
            if isinstance(msg.src, Gst.Pipeline):
                old_state, new_state, pending_state = msg.parse_state_changed()
                self._logger.info(("Pipeline state changed from %s to %s." % (old_state.value_nick, new_state.value_nick)))
                if old_state.value_nick == "paused" and new_state.value_nick == "ready":
                    self._logger.info("stopping bus message loop")
                    loop.quit()
                    return False
        elif t == Gst.MessageType.LATENCY:
            if self._pipeline:
                try:
                    self._pipeline.recalculate_latency()
                except Exception as e:
                    self._logger.warning("failed to recalculate warning, exception: %s" % str(e))
        return True

    async def _handle_bus_calls(self) -> None:
        self._logger.debug("Starting bus call loop")

        if self._pipeline is not None:
            bus = self._pipeline.get_bus()
            bus.add_watch(GLib.PRIORITY_DEFAULT, self.bus_message_cb, self._loop)
            self._loop.run()
            bus.remove_watch()
        else:
            self._logger.warning("pipeline is None")

        self._logger.debug("Stopping bus call loop")

    """
    def find_remote_producer(self) -> None:
        peer_audio_id = ""

        while True:
            try:
                peer_audio_id = utils.find_producer_peer_id_by_name(
                    self._signalling_host, self._signalling_port, self._peer_audio_name
                )
                self._logger.info(f"Found peer {self._peer_audio_name} with id {peer_audio_id}")
                break
            except KeyError:
                time.sleep(3)

        while self._pipeline is None:
            self._logger.debug("wait for pipe to be ready")
            time.sleep(1)

        if self._pipeline is not None:
            with self._mutex:
                self._pipeline.set_state(Gst.State.PAUSED)
                self._add_webrtcsrc(peer_audio_id)
            self.start()
    """

    async def _removing_webrtcsrc(self) -> None:
        if self._pipeline is not None:
            async with self._mutex:
                self._logger.debug("removing webrtcsrc")
                self._pipeline.set_state(Gst.State.PAUSED)
                if self._webrtcdsp:
                    self._alsasrc.unlink(self._webrtcdsp)
                    self._alsasrc.link(self._queue_audio)
                    self._queue_audio.sync_state_with_parent()
                for comp in self._components_webrtcsrc:
                    comp.set_state(Gst.State.NULL)
                    self._pipeline.remove(comp)
                self._components_webrtcsrc.clear()
            await self.start()
        self._logger.debug("webrtcsrc removed")

    async def _adding_webrtcsrc(self, peer_audio_id: str) -> None:
        while self._pipeline is None:
            self._logger.debug("waiting for pipe to be ready")
            await asyncio.sleep(0.5)
        if self._pipeline is not None:
            async with self._mutex:
                self._pipeline.set_state(Gst.State.PAUSED)
                self._add_webrtcsrc(peer_audio_id)
            await self.start()

    async def _handle_peer_status_changed(self, peer_id: str, roles: List[str], meta: Dict[str, str]) -> None:
        self._logger.debug(f'Peer "{peer_id}" changed roles to {roles} with meta {meta}')
        if meta is None:
            pass
        elif peer_id == self._peer_audio_id and meta["name"] == self._peer_audio_name:
            self._peer_audio_id = ""
            await self._removing_webrtcsrc()
            self._logger.info(f"Operator {self._peer_audio_id} is disconnected")
        elif self._peer_audio_id == "" and meta["name"] == self._peer_audio_name and "producer" in roles:
            self._peer_audio_id = peer_id
            await self._adding_webrtcsrc(self._peer_audio_id)
            self._logger.info(f"Operator is connected with id : {self._peer_audio_id}")
