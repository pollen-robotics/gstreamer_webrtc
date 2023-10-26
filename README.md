# Gstreamer WebRTC

[![Code style: black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black) ![linter](https://github.com/pollen-robotics/gstreamer_webrtc/actions/workflows/lint.yml/badge.svg)

This python code streams video from a Luxonis Camera, and audio from a microphone. It can also consume and play back an audio stream from a remote peer. This piece of software is based on the [gstreamer webrtc plugin](https://gitlab.freedesktop.org/gstreamer/gst-plugins-rs/-/tree/main/net/webrtc).

## Installation

The dependencies are listed in the ```setup.cfg``` file and will be installed if you install this package locally with:
```
pip install -e .[dev]
```
use *[dev]* for optional development tools.


## Usage

The installation provides the executable `streaming_service`. Use the `--help` option for more infos about the configuration.

### Examples

Stream audio only

```console
streaming_service  --config config/CONFIG_OAK.json producer --name robot --verbose --stream audio
```

Stream video only

```console
streaming_service  --config config/CONFIG_OAK.json producer --name robot --verbose --stream video
```       

Steam audio and video, and playback sound from remote peer
```console
streaming_service --config config/CONFIG_OAK.json producer --name robot --verbose --stream audiovideo --remote-producer-name UnityClient
```
