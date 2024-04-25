import logging

import gi
import numpy as np

gi.require_version("Gst", "1.0")
import asyncio
import subprocess
from threading import Thread
from typing import Dict, List, Optional, Tuple

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
        self._appsrc_left: Optional[Gst.Element] = None
        self._appsrc_right: Optional[Gst.Element] = None
        self._lowlatencyaudio = lowlatencyaudio
        self._localnetwork = localnetwork
        self._peer_audio_name = peer_audio_name
        self._peer_audio_id = ""
        self._congestion = congestion
        self._aec = aec

        self._thread_bus_calls: Optional[Thread] = None
        self._loop = GLib.MainLoop()

        self._asyncio_loop = asyncio.get_running_loop()

        self._webrtcsrc: Optional[Gst.Element] = None
        self._webrtcsrc_count: int = -1

        self._peer_audio_listener: Optional[GstSignallingListener] = None
        self._listener_task = None
        if self._peer_audio_name is not None:
            self._peer_audio_listener = GstSignallingListener(
                host=signalling_host,
                port=signalling_port,
                name=self._peer_audio_name,
            )
            self._peer_audio_listener.on("PeerStatusChanged", self._handle_peer_status_changed)
            self._listener_task = asyncio.create_task(self._peer_audio_listener.serve4ever())

        # usb device : 4c4a:4155 Jieli Technology UACDemoV1.0
        self.VENDOR_ID = "4c4a"
        self.PRODUCT_ID = "4155"
        self._usb_monitor_task: Optional[asyncio.Task] = None
        self._usb_speaker_connected = False  # self._is_usb_speaker_connected()
        """
        if self._usb_speaker_connected:
            # assuming that the device is well connected at the startup
            # if not we consider that the rode outputs the sound and we don't need this
            self._usb_monitor_task = asyncio.create_task(self._monitor_usb())
        """
        Gst.init(None)
        self._pipeline = Gst.Pipeline.new()

    def __del__(self) -> None:
        Gst.deinit()

    async def cleanup(self) -> None:
        if self._listener_task:
            self._listener_task.cancel()
        if self._usb_monitor_task:
            self._usb_monitor_task.cancel()
        if self._peer_audio_listener:
            await self._peer_audio_listener.close()

    def get_appsrc(self, name: str) -> Optional[Gst.Element]:
        if name == "left":
            return self._appsrc_left
        elif name == "right":
            return self._appsrc_right
        else:
            self._logger.warning("Unknow appsrc name : f{name}. Should be left or right.")
            return None

    def _add_webrtcink(self) -> Gst.Element:
        webrtcsink = Gst.ElementFactory.make("webrtcsink")
        assert webrtcsink is not None
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
        self._pipeline.add(webrtcsink)
        return webrtcsink

    def _add_appsrc(self, name: str, cam_latency_ns: int) -> Gst.Element:
        appsrc = Gst.ElementFactory.make("appsrc")
        assert appsrc is not None
        appsrc.set_property("name", name)
        appsrc.set_property("format", Gst.Format.TIME)
        appsrc.set_property("is-live", True)
        appsrc.set_property("leaky-type", 2)
        appsrc.set_property("min-latency", cam_latency_ns)
        appsrc.set_property("max-buffers", 100)
        self._logger.info(f"Video latency configured to {cam_latency_ns}")
        self._pipeline.add(appsrc)
        return appsrc

    def _add_h264parse(self) -> Gst.Element:
        h264parse = Gst.ElementFactory.make("h264parse")
        assert h264parse is not None
        self._pipeline.add(h264parse)
        return h264parse

    def _add_queue(self) -> Gst.Element:
        queue = Gst.ElementFactory.make("queue")
        assert queue is not None
        self._pipeline.add(queue)
        return queue

    def _add_audioconvert(self) -> Gst.Element:
        audioconvert = Gst.ElementFactory.make("audioconvert")
        assert audioconvert is not None
        self._pipeline.add(audioconvert)
        return audioconvert

    def _add_audioresample(self) -> Gst.Element:
        audioresample = Gst.ElementFactory.make("audioresample")
        assert audioresample is not None
        self._pipeline.add(audioresample)
        return audioresample

    def _add_alsasrc(self, lowlatencydevice: bool = True) -> Gst.Element:
        alsasrc = Gst.ElementFactory.make("alsasrc")
        assert alsasrc is not None
        if lowlatencydevice:
            alsasrc.set_property("device", "lowlatencysrc")
        alsasrc.set_property("buffer-time", 30000)
        alsasrc.set_property("latency-time", 10000)
        self._pipeline.add(alsasrc)
        return alsasrc

    def _add_alsasink(self, lowlatencydevice: bool = True) -> Gst.Element:
        alsasink = Gst.ElementFactory.make("alsasink")
        assert alsasink is not None
        alsasink.set_property("name", "speaker")
        if lowlatencydevice:
            alsasink.set_property("device", "lowlatencysink")
        alsasink.set_property("buffer-time", 30000)
        alsasink.set_property("latency-time", 10000)
        self._pipeline.add(alsasink)
        return alsasink

    def _add_audiotestsrc(self) -> Gst.Element:
        """audio silent stream used to feed alsasink before user connects"""
        audiotestsrc = Gst.ElementFactory.make("audiotestsrc")
        assert audiotestsrc is not None
        audiotestsrc.set_property("wave", "silence")
        audiotestsrc.set_property("is-live", True)
        self._pipeline.add(audiotestsrc)
        return audiotestsrc

    def _add_audiomixer(self) -> Gst.Element:
        audiomixer = Gst.ElementFactory.make("audiomixer")
        assert audiomixer is not None
        audiomixer.set_property("name", "audiomixer-in")
        self._pipeline.add(audiomixer)
        return audiomixer

    def _add_valve(self) -> Gst.Element:
        valve = Gst.ElementFactory.make("valve")
        assert valve is not None
        valve.set_property("name", "safety-valve")
        self._pipeline.add(valve)
        return valve

    def _add_opus_enc(self) -> Tuple[Gst.Element, Gst.Element]:
        opusenc = Gst.ElementFactory.make("opusenc")
        assert opusenc is not None
        opusenc.set_property("audio-type", "restricted-lowdelay")
        opusenc.set_property("frame-size", 10)
        self._pipeline.add(opusenc)

        audio_caps = Gst.caps_from_string("audio/x-opus")
        assert audio_caps is not None
        audio_caps.set_value("channels", 2)
        audio_caps.set_value("rate", 48000)
        audio_caps_capsfilter = Gst.ElementFactory.make("capsfilter")
        assert audio_caps_capsfilter is not None
        audio_caps_capsfilter.set_property("caps", audio_caps)
        self._pipeline.add(audio_caps_capsfilter)

        return opusenc, audio_caps_capsfilter

    def _configure_webrtcbin(self, webrtcsrc: Gst.Element) -> None:
        if self._localnetwork and isinstance(webrtcsrc, Gst.Bin):
            webrtcbin_name = "webrtcbin" + str(self._webrtcsrc_count)
            webrtcbin = webrtcsrc.get_by_name(webrtcbin_name)
            assert webrtcbin is not None
            # jitterbuffer has a default 200 ms buffer. Should be ok to lower this in localnetwork config
            webrtcbin.set_property("latency", 50)

    def _webrtcsrc_pad_added_cb(self, webrtcsrc: Gst.Element, pad: Gst.Pad) -> None:
        if pad is not None and pad.get_name().startswith("audio"):  # type: ignore[union-attr]
            self._logger.info("Connecting audio client")

            self._configure_webrtcbin(webrtcsrc)
            audiomixer = self._pipeline.get_by_name("audiomixer-in")
            assert audiomixer is not None
            template = audiomixer.get_pad_template("sink_%u")
            assert template is not None
            mixer_pad = audiomixer.request_pad(template)
            assert mixer_pad is not None
            pad.link(mixer_pad)

            GLib.timeout_add_seconds(5, self.dump_latency)

    def _add_webrtcsrc(self, peer_audio_id: str) -> Gst.Element:
        webrtcsrc = Gst.ElementFactory.make("webrtcsrc")
        assert webrtcsrc is not None
        self._webrtcsrc_count += 1

        if self._localnetwork:
            webrtcsrc.set_property("stun-server", None)

        signaller = webrtcsrc.get_property("signaller")
        signaller.set_property("producer-peer-id", peer_audio_id)
        signaller.set_property("uri", f"ws://{self._signalling_host}:{self._signalling_port}")

        webrtcsrc.connect("pad-added", self._webrtcsrc_pad_added_cb)

        self._pipeline.add(webrtcsrc)
        webrtcsrc.sync_state_with_parent()
        return webrtcsrc

    def _add_webrtcdsp(self) -> Gst.Element:
        webrtcdsp = Gst.ElementFactory.make("webrtcdsp")
        assert webrtcdsp is not None
        if self._aec == "off" or self._peer_audio_name is None:
            webrtcdsp.set_property("echo-cancel", False)
        elif self._aec == "strong":
            webrtcdsp.set_property("delay-agnostic", True)
            webrtcdsp.set_property("echo-suppression-level", 2)
        # else normal is default parameters for webrtcdsp
        self._pipeline.add(webrtcdsp)
        return webrtcdsp

    def _add_webrtcechoprobe(self) -> Gst.Element:
        webrtcechoprobe = Gst.ElementFactory.make("webrtcechoprobe")
        assert webrtcechoprobe is not None
        self._pipeline.add(webrtcechoprobe)
        return webrtcechoprobe

    def _set_stereo_video(self, webrtcsink: Gst.Element, cam_latency: int) -> None:
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

    def _set_stereo_audio(self, webrtcsink: Gst.Element) -> None:
        self._logger.info("Set up stereo audio pipeline")
        alsasrc = self._add_alsasrc(self._lowlatencyaudio)
        self._webrtcdsp = self._add_webrtcdsp()
        queue_audio = self._add_queue()
        opusenc, audio_caps = self._add_opus_enc()

        if not Gst.Element.link(alsasrc, self._webrtcdsp):
            self._logger.error("Failed to link alsasrc -> webrtdsp")
        if not Gst.Element.link(self._webrtcdsp, queue_audio):
            self._logger.error("Failed to link webrtcdsp -> queue")
        if not Gst.Element.link(queue_audio, opusenc):
            self._logger.error("Failed to link queue -> opusenc")
        if not Gst.Element.link(opusenc, audio_caps):
            self._logger.error("Failed to link opusenc -> caps")
        if not Gst.Element.link(audio_caps, webrtcsink):
            self._logger.error("Failed to link caps -> webrtcsink")

    def _set_audio_playback(self) -> None:
        audiotestsrc = self._add_audiotestsrc()
        audiomixer = self._add_audiomixer()
        webrtcechoprobe = self._add_webrtcechoprobe()
        audioconvert = self._add_audioconvert()
        audioresample = self._add_audioresample()
        valve = self._add_valve()
        alsasink = self._add_alsasink(self._lowlatencyaudio)

        if not Gst.Element.link(audiotestsrc, audiomixer):
            self._logger.error("Failed to link audiotestsrc -> audiomixer")
        if not Gst.Element.link(audiomixer, webrtcechoprobe):
            self._logger.error("Failed to link audiomixer -> webrtcprobe")
        if not Gst.Element.link(webrtcechoprobe, audioconvert):
            self._logger.error("Failed to link webrtcprobe -> audioconvert")
        if not Gst.Element.link(audioconvert, audioresample):
            self._logger.error("Failed to link audioconvert -> audioresample")
        if not Gst.Element.link(audioresample, valve):
            self._logger.error("Failed to link audioresample -> valve")
        if not Gst.Element.link(valve, alsasink):
            self._logger.error("Failed to link valve -> alsasink")

    def make_pipeline(self, cam_latency: int = 0) -> None:
        webrtcsink = self._add_webrtcink()

        if self._stream_type == "video" or self._stream_type == "audiovideo":
            self._set_stereo_video(webrtcsink, cam_latency)
        if self._stream_type == "audio" or self._stream_type == "audiovideo":
            self._set_stereo_audio(webrtcsink)
        if self._peer_audio_name is not None:
            self._set_audio_playback()

        ret = self._pipeline.set_state(Gst.State.READY)
        if ret not in [Gst.StateChangeReturn.SUCCESS, Gst.StateChangeReturn.ASYNC]:
            self._logger.error(f"Failed to transition pipeline to READY: {ret}")

    def push_frame(self, appsrc: Gst.Element, data: npt.NDArray[np.uint8], latency_ns: int = 0) -> None:
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
        query = Gst.Query.new_latency()
        self._pipeline.query(query)
        self._logger.info(f"Pipeline latency {query.parse_latency()}")

    async def start(self) -> None:
        ret = self._pipeline.set_state(Gst.State.PLAYING)
        if ret not in [Gst.StateChangeReturn.SUCCESS, Gst.StateChangeReturn.ASYNC]:
            self._logger.error(f"Failed to transition pipeline to PLAYING: {ret}")

        if self._thread_bus_calls is None:
            self._thread_bus_calls = Thread(target=self._handle_bus_calls, daemon=True)
            self._thread_bus_calls.start()

        GLib.timeout_add_seconds(10, self.dump_latency)

    async def stop(self) -> None:
        self._pipeline.set_state(Gst.State.NULL)
        self._logger.info("Pipeline stopped")
        if self._thread_bus_calls:
            self._loop.quit()
            self._thread_bus_calls.join()
            self._thread_bus_calls = None

    def bus_message_cb(self, bus: Gst.Bus, msg: Gst.Message, loop) -> bool:  # type: ignore[no-untyped-def]
        t = msg.type
        if t == Gst.MessageType.EOS:
            self._logger.error("End-of-stream")
            return False
        elif t == Gst.MessageType.ERROR:
            err, debug = msg.parse_error()
            self._logger.error("Error: %s: %s" % (err, debug))

            if "The device has been disconnected" in str(err):
                self._logger.error("USB speaker disconnected. Dropping output audio")
                valve = self._pipeline.get_by_name("safety-valve")
                assert valve is not None
                valve.set_property("drop", True)
                alsasink = self._pipeline.get_by_name("speaker")
                assert alsasink is not None
                alsasink.set_state(Gst.State.NULL)
                """
                ret = self._pipeline.set_state(Gst.State.NULL)
                if ret not in [Gst.StateChangeReturn.SUCCESS, Gst.StateChangeReturn.ASYNC]:
                    self._logger.error(f"Failed to transition pipeline to READY: {ret}")

                self._alsasink.set_state(Gst.State.NULL)
                self._pipeline.remove(self._alsasink)
                self._fakesink = Gst.ElementFactory.make("fakesink")
                assert self._fakesink
                self._pipeline.add(self._fakesink)

                if not Gst.Element.link(self._audioresample, self._fakesink):
                    self._logger.error("Failed to link audioresample -> fakesink")

                ret = self._pipeline.set_state(Gst.State.PLAYING)
                if ret not in [Gst.StateChangeReturn.SUCCESS, Gst.StateChangeReturn.ASYNC]:
                    self._logger.error(f"Failed to transition pipeline to READY: {ret}")
                """
                # self._usb_speaker_connected = False
                # self._usb_monitor_task = self._asyncio_loop.create_task(self._attempt_to_reconnect())
                return True
            else:
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

    def _handle_bus_calls(self) -> None:
        self._logger.debug("Starting bus call loop")
        bus = self._pipeline.get_bus()
        bus.add_watch(GLib.PRIORITY_DEFAULT, self.bus_message_cb, self._loop)
        self._loop.run()  # type: ignore[no-untyped-call]
        bus.remove_watch()

        self._logger.debug("Stopping bus call loop")

    async def _removing_webrtcsrc(self) -> None:
        self._peer_audio_id = ""
        if self._webrtcsrc is not None:
            self._webrtcsrc.set_state(Gst.State.NULL)
            self._pipeline.remove(self._webrtcsrc)
            self._webrtcsrc = None
        self._logger.debug("webrtcsrc removed")

    async def _adding_webrtcsrc(self, peer_audio_id: str) -> None:
        while True:
            state_change_return, state, pending = self._pipeline.get_state(Gst.CLOCK_TIME_NONE)
            if state_change_return == Gst.StateChangeReturn.SUCCESS and state == Gst.State.PLAYING:
                self._logger.debug("Pipeline is ready and playing")
                break
            elif state_change_return == Gst.StateChangeReturn.FAILURE:
                self._logger.error("Failed to get the pipeline state, it might be in an error state")
                break
            else:
                self._logger.debug(f"Pipeline is not ready yet, current state: {state.value_nick}")
                await asyncio.sleep(0.5)
        self._webrtcsrc = self._add_webrtcsrc(peer_audio_id)

    async def _handle_peer_status_changed(self, peer_id: str, roles: List[str], meta: Dict[str, str]) -> None:
        self._logger.debug(f'Peer "{peer_id}" changed roles to {roles} with meta {meta}')
        if meta is None:
            pass
        elif peer_id == self._peer_audio_id and meta["name"] == self._peer_audio_name:
            await self._removing_webrtcsrc()
            self._logger.info(f"Operator {self._peer_audio_id} is disconnected")
        elif self._peer_audio_id == "" and meta["name"] == self._peer_audio_name and "producer" in roles:
            # while not self._usb_speaker_connected:
            #    self._logger.info("UnityClient available. Waiting for usb speaker to connect...")
            #    await asyncio.sleep(1)
            self._peer_audio_id = peer_id
            await self._adding_webrtcsrc(self._peer_audio_id)
            self._logger.info(f"Operator is connected with id : {self._peer_audio_id}")

    def _is_usb_speaker_connected(self) -> bool:
        output = subprocess.check_output(["lsusb"])
        if f"{self.VENDOR_ID}:{self.PRODUCT_ID}" in output.decode("utf-8"):
            return True
        else:
            return False

    async def _monitor_usb(self) -> None:
        self._logger.info("Starting monitoring usb ...")
        while True:
            self._usb_speaker_connected = self._is_usb_speaker_connected()
            if not self._usb_speaker_connected:
                self._logger.warning("USB speaker has been disconnected")
                await self._removing_webrtcsrc()
            await asyncio.sleep(2)

        self._logger.info("Stoping monitoring usb")

    async def _attempt_to_reconnect(self) -> None:
        # await self._removing_webrtcsrc()
        while not self._is_usb_speaker_connected():
            await asyncio.sleep(10)
            self._logger.info("Wait for speaker to reconnect ...")
        """
        if self._peer_audio_id == "":
            self._logger.info("No peer audio id to reconnect to")
        else:
            await self._adding_webrtcsrc(self._peer_audio_id)
        """
