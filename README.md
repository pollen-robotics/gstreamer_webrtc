# Python Guidelines and Coding style

[![Code style: black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black) 



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
streaming_service  --config config/CONFIG_OAK.json producer --name robot --verbose --rescale 720p --stream audio
```

Stream video only

```console
streaming_service  --config config/CONFIG_OAK.json producer --name robot --verbose --rescale 720p --stream video
```       

Steam audio and video, and playback sound from remote peer
```console
streaming_service --config config/CONFIG_OAK.json producer --name robot --verbose --rescale 720p --stream audiovideo --remote-producer-name UnityClient
```