import asyncio
import logging

from pollen_vision.camera_wrappers.depthai.cam_config import CamConfig
from rclpy.node import Node
from sensor_msgs.msg import CameraInfo
from sensor_msgs.msg._compressed_image import CompressedImage


class ROSPublisher(Node):  # type: ignore[misc]
    def __init__(self, cam_config: CamConfig, side: str, asyncio_loop: asyncio.AbstractEventLoop) -> None:
        super().__init__(f"teleop_camera_publisher_{side}")
        self._logger = logging.getLogger(__name__)
        self._clock = self.get_clock()
        self._side = side

        self._camera_publisher = self.create_publisher(CompressedImage, f"teleop_camera/{self._side}_image/compressed", 1)
        self._logger.info(f'Launching "{self._camera_publisher.topic_name}" publisher.')

        self._compr_img = CompressedImage()
        self._compr_img.format = "jpeg"
        self._compr_img.header.frame_id = f"{side}_camera"

        self._camera_info_publisher = self.create_publisher(CameraInfo, f"teleop_camera/{self._side}_image/camera_info", 5)
        self._logger.info(f'Launching "{self._camera_info_publisher.topic_name}" publisher.')

        self._camera_info = CameraInfo()
        self._camera_info.header.frame_id = f"{side}_camera"
        height, width, distortion_model, D, K, R, P = cam_config.to_ROS_msg(side)
        self._camera_info.height = height
        self._camera_info.width = width
        self._camera_info.distortion_model = distortion_model
        self._camera_info.d = D
        self._camera_info.k = K
        self._camera_info.r = R
        self._camera_info.p = P

        asyncio.run_coroutine_threadsafe(self._publish_camera_info(side), asyncio_loop)

        self._logger.info(f"Node teleop_camera_publisher_{side} ready!")

    def publish_img(self, frame: bytes) -> None:
        """Read image from the requested side and publishes it."""
        self._compr_img.header.stamp = self._clock.now().to_msg()
        self._compr_img.data = frame
        self._camera_publisher.publish(self._compr_img)

    async def _publish_camera_info(self, side: str) -> None:
        """Publish camera info for the requested side."""
        while True:
            self._camera_info.header.stamp = self._clock.now().to_msg()
            self._camera_info_publisher.publish(self._camera_info)
            await asyncio.sleep(1)
