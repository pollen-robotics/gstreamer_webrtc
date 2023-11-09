import datetime
import json
import logging
from typing import Dict, List, Optional, Tuple

import cv2
import depthai as dai
import numpy as np
import numpy.typing as npt

stringToCam = {
    "CAM_A": dai.CameraBoardSocket.CAM_A,
    "CAM_B": dai.CameraBoardSocket.CAM_B,
    "CAM_C": dai.CameraBoardSocket.CAM_C,
    "CAM_D": dai.CameraBoardSocket.CAM_D,
}

# Conventions :
# - cam_name is the friendly name of the camera (i.e. "left")
# - cam_id is the id of the camera (i.e. "CAM_A")
# - cam poses is always right to left (right expressed in the left frame)


class FFCWrapper:
    def __init__(
        self,
        config: str,
        fps: int = 30,
        rescale: str = "no",
        hardware_sync: bool = True,
        hardware_rectify: bool = True,
        codec: str = "h264",
        usb2: bool = False,
        exposure_params: tuple = None,  # (exposure_time, iso). None means auto
    ) -> None:
        assert codec in ["h264", "h265"]
        assert rescale in ["no", "720p"]

        self._logger = logging.getLogger(__name__)
        self.fps = fps
        self.rescale = rescale
        if rescale == "720p":
            self.resolution = (1280, 720)
        self.hardware_sync = hardware_sync
        self.hardware_rectify = hardware_rectify
        self.codec = codec
        self.exposure_params = exposure_params
        if self.exposure_params is not None:
            iso = self.exposure_params[1]
            assert 100 <= iso <= 1600

        self.config = json.load(open(config, "r"))
        self.cam_type = self.config["cam_type"]

        self.data: Dict[str, npt.NDArray[np.uint8]] = {}
        self.latency: Dict[str, float] = {}
        self.ts: Dict[str, datetime.timedelta] = {}

        self.cam_info = self.config["sockets"]
        self.cam_info_reverse = {}
        for cam_id, cam_name in self.cam_info.items():
            self.cam_info_reverse[cam_name] = cam_id

        self._logger.info(f"Hardware synchronization is {self.hardware_sync}")
        self._logger.info(f"Hardware rectification is {self.hardware_rectify}")
        self._logger.info(f"Rescale is : {rescale}")
        self._logger.info(f"FPS is : {self.fps}")
        self._logger.info(f"Cam type is : {self.cam_type}")

        self.calib = None
        if self.hardware_rectify:
            self.calib = dai.Device().readCalibration()

        pipeline, self.cams = self.create_pipeline()

        self.device = dai.Device(
            pipeline,
            maxUsbSpeed=(dai.UsbSpeed.HIGH if usb2 else dai.UsbSpeed.SUPER_PLUS),
        )

        self.queue = {}
        for cam_id in self.cams.keys():
            self.queue[cam_id] = self.device.getOutputQueue(
                cam_id, maxSize=30, blocking=True
            )

    def _set_up_cam_node(
        self, pipeline: dai.Pipeline, cam_id: str
    ) -> dai.node.ColorCamera:
        cam_node = pipeline.createColorCamera()
        cam_node.initialControl.setManualFocus(135)  # Needed ?
        cam_node.setBoardSocket(stringToCam[cam_id])
        if self.exposure_params is not None:
            print("setting exposure ", *self.exposure_params)
            cam_node.initialControl.setManualExposure(*self.exposure_params)

        if self.cam_type == "ov" or self.cam_type == "oak":
            cam_node.setResolution(dai.ColorCameraProperties.SensorResolution.THE_800_P)
        elif self.cam_type == "imx296":
            cam_node.setResolution(
                dai.ColorCameraProperties.SensorResolution.THE_1440X1080
            )
        elif self.cam_type == "imx378":
            cam_node.setResolution(
                dai.ColorCameraProperties.SensorResolution.THE_1080_P
            )
        elif self.cam_type == "AR":
            cam_node.setResolution(
                dai.ColorCameraProperties.SensorResolution.THE_1200_P
            )
        else:
            self._logger.error(f"ERROR : unknown cam type {self.cam_type}")
            exit()

        return cam_node

    def _set_hardware_sync(self, cam_id: str, cam_node: dai.node.ColorCamera) -> None:
        # Explicit harware sync needed for CAM_A/CAM_D pair
        # https://discuss.luxonis.com/d/934-ffc-4p-hardware-synchronization/4
        if "CAM_D" in self.cam_info.keys():
            if cam_id == "CAM_D":
                cam_node.initialControl.setFrameSyncMode(
                    dai.CameraControl.FrameSyncMode.OUTPUT
                )
            else:
                cam_node.initialControl.setFrameSyncMode(
                    dai.CameraControl.FrameSyncMode.INPUT
                )

    def _set_manip_rescale(self, pipeline: dai.Pipeline) -> dai.node.ImageManip:
        manipRescale = pipeline.createImageManip()

        manipRescale.initialConfig.setResizeThumbnail(*self.resolution)

        manipRescale.setMaxOutputFrameSize(self.resolution[0] * self.resolution[1] * 3)
        if not self.hardware_rectify:
            manipRescale.initialConfig.setFrameType(dai.ImgFrame.Type.NV12)
        return manipRescale

    def _set_hardware_rectify(
        self, pipeline: dai.Pipeline, cam_id: str
    ) -> dai.node.ImageManip:
        manipRectify = pipeline.createImageManip()

        mesh, meshWidth, meshHeight = self.get_mesh(
            self.cam_info[cam_id],
            self.resolution,
        )
        manipRectify.setWarpMesh(mesh, meshWidth, meshHeight)

        manipRectify.setMaxOutputFrameSize(self.resolution[0] * self.resolution[1] * 3)

        manipRectify.initialConfig.setFrameType(dai.ImgFrame.Type.NV12)

        return manipRectify

    def _linking(
        self,
        cam_node: dai.node.ColorCamera,
        cam_id: str,
        pipeline: dai.Pipeline,
        manipRescale: Optional[dai.node.ImageManip],
        manipRectify: Optional[dai.node.ImageManip],
    ) -> dai.node.XLinkOut:
        if manipRectify and manipRescale and self.rescale == "720p":
            cam_node.isp.link(manipRescale.inputImage)
            manipRescale.out.link(manipRectify.inputImage)
        elif manipRectify:
            cam_node.isp.link(manipRectify.inputImage)
        elif manipRescale and self.rescale == "720p":
            cam_node.isp.link(manipRescale.inputImage)

        out = pipeline.createXLinkOut()
        out.setStreamName(cam_id)
        return out

    def create_pipeline(self) -> Tuple[dai.Pipeline, Dict[str, dai.node.ColorCamera]]:
        pipeline = dai.Pipeline()
        cams = {}

        for cam_id in self.cam_info.keys():
            cam_node = self._set_up_cam_node(pipeline, cam_id)

            if self.rescale == "no":
                self.resolution = cam_node.getIspSize()

            cam_node.setFps(self.fps)

            # if self.config["inverted"]:
            #     cam_node.setImageOrientation(dai.CameraImageOrientation.ROTATE_180_DEG)
            cam_node.setInterleaved(False)

            # If sockets are B/C, nothing to do. Else activate this
            if self.hardware_sync:
                self._set_hardware_sync(cam_id, cam_node)

            manipRescale = None
            if self.rescale == "720p":
                manipRescale = self._set_manip_rescale(pipeline)

            manipRectify = None
            if self.hardware_rectify:
                manipRectify = self._set_hardware_rectify(pipeline, cam_id)
            # Linking
            out = self._linking(cam_node, cam_id, pipeline, manipRescale, manipRectify)

            encoder = pipeline.create(dai.node.VideoEncoder)
            profile = (
                dai.VideoEncoderProperties.Profile.H264_MAIN
            )  # TOdo select encoder
            encoder.setDefaultProfilePreset(self.fps, profile)
            # from https://support.google.com/youtube/answer/2853702?hl=en
            encoder.setKeyframeFrequency(self.fps * 3)  # every 3s
            # encoder.setNumBFrames(2)
            if manipRectify:
                manipRectify.out.link(encoder.input)
            elif manipRescale and self.rescale == "720p":
                manipRescale.out.link(encoder.input)
            else:
                cam_node.isp.link(
                    encoder.input
                )  # Maybe cam_node.video instead of isp ? (was video before)

            encoder.bitstream.link(out.input)

            cams[cam_id] = cam_node

        return pipeline, cams

    def get_data(
        self,
    ) -> Tuple[
        Dict[str, npt.NDArray[np.uint8]],
        Dict[str, float],
        Dict[str, datetime.timedelta],
    ]:
        self._update_data()
        return self.data, self.latency, self.ts

    def _update_data(self) -> None:
        pkts = {}
        for cam in self.cams:
            pkts[cam] = self.queue[cam].get()
        for cam, pkt in pkts.items():
            self.data[self.cam_info[cam]] = pkt.getData()  # type: ignore[attr-defined]
            self.latency[self.cam_info[cam]] = (
                dai.Clock.now() - pkt.getTimestamp()  # type: ignore[attr-defined, call-arg]
            ).total_seconds() * 1000
            self.ts[self.cam_info[cam]] = pkt.getTimestamp()  # type: ignore[attr-defined]

    def get_mesh(
        self, cam_name: str, resolution: Tuple[int, int]
    ) -> Tuple[List[Tuple[float, float]], int, int]:
        l_CS = stringToCam[self.cam_info_reverse["left"]]
        r_CS = stringToCam[self.cam_info_reverse["right"]]

        if not self.calib:
            self._logger.error("camera not calibrated")
            exit()

        left_K = np.array(
            self.calib.getCameraIntrinsics(l_CS, resolution[0], resolution[1])
        )
        left_D = np.array(self.calib.getDistortionCoefficients(l_CS))
        R1 = np.array(self.calib.getStereoLeftRectificationRotation())
        right_K = np.array(
            self.calib.getCameraIntrinsics(r_CS, resolution[0], resolution[1])
        )
        right_D = np.array(self.calib.getDistortionCoefficients(r_CS))
        R2 = np.array(self.calib.getStereoRightRectificationRotation())

        mapXL, mapYL = cv2.initUndistortRectifyMap(
            left_K, left_D, R1, right_K, resolution, cv2.CV_32FC1  # type: ignore[attr-defined]
        )
        mapXR, mapYR = cv2.initUndistortRectifyMap(
            right_K, right_D, R2, right_K, resolution, cv2.CV_32FC1  # type: ignore[attr-defined]
        )

        mapX = mapXL if cam_name == "left" else mapXR
        mapY = mapYL if cam_name == "left" else mapYR

        meshCellSize = 16
        mesh0 = []
        # Creates subsampled mesh which will be loaded on to device to undistort the image
        for y in range(mapX.shape[0] + 1):  # iterating over height of the image
            if y % meshCellSize == 0:
                rowLeft = []
                for x in range(mapX.shape[1]):  # iterating over width of the image
                    if x % meshCellSize == 0:
                        if y == mapX.shape[0] and x == mapX.shape[1]:
                            rowLeft.append(mapX[y - 1, x - 1])
                            rowLeft.append(mapY[y - 1, x - 1])
                        elif y == mapX.shape[0]:
                            rowLeft.append(mapX[y - 1, x])
                            rowLeft.append(mapY[y - 1, x])
                        elif x == mapX.shape[1]:
                            rowLeft.append(mapX[y, x - 1])
                            rowLeft.append(mapY[y, x - 1])
                        else:
                            rowLeft.append(mapX[y, x])
                            rowLeft.append(mapY[y, x])
                if (mapX.shape[1] % meshCellSize) % 2 != 0:
                    rowLeft.append(0)
                    rowLeft.append(0)

                mesh0.append(rowLeft)

        mesh_np = np.array(mesh0)
        meshWidth = mesh_np.shape[1] // 2
        meshHeight = mesh_np.shape[0]
        mesh_np.resize(meshWidth * meshHeight, 2)

        mesh = list(map(tuple, mesh_np))

        return mesh, meshWidth, meshHeight  # type: ignore [return-value]

    def close(self) -> None:
        if self.device:
            self.device.close()
