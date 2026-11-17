# Third-party notices

The MIT license covers original SO2 Clipper code and documentation. It does not
relicense third-party software, model weights, game artwork, or trademarks.

## Runtime dependencies

The release ZIP contains Python source, PowerShell entry points, small HUD
matching templates, and documentation. It does **not** bundle Python, FFmpeg,
PyTorch, EasyOCR model weights, or their binary distributions. Setup downloads
or reuses these dependencies separately and preserves their own notices.

| Component | Upstream license / information | How it is used |
|---|---|---|
| EasyOCR | [Apache-2.0](https://github.com/JaidedAI/EasyOCR/blob/master/LICENSE) | Local OCR; model files are downloaded from upstream, not included in this repository |
| PyTorch / torchvision | [PyTorch BSD-style notices](https://github.com/pytorch/pytorch/blob/main/LICENSE), [torchvision BSD-3-Clause](https://github.com/pytorch/vision/blob/main/LICENSE) | Local inference, with CPU or supported NVIDIA acceleration |
| OpenCV | [Apache-2.0 for OpenCV 4.5+](https://opencv.org/license/) | Frame processing and computer vision |
| NumPy | [BSD-3-Clause](https://github.com/numpy/numpy/blob/main/LICENSE.txt) | Array processing |
| Pillow | [HPND and historical notices](https://github.com/python-pillow/Pillow/blob/main/LICENSE) | Image handling |
| Python | [Python license and notices](https://docs.python.org/3/license.html) | Runtime; optionally installed by the Windows setup script |
| FFmpeg | [License and legal information](https://ffmpeg.org/legal.html), [Gyan build information](https://www.gyan.dev/ffmpeg/builds/) | Video decoding and encoding; the selected Gyan essentials build includes GPL components |

Additional Python packages retain their upstream licenses. Exact runtime pins
are in `skills/so2-clipper/scripts/requirements.txt`; setup records the installed
package versions in its local `installed.txt`. This file is an overview, not a
replacement for the license notices supplied by each installed distribution.

## Game material and screenshots

Standoff 2 / 《对峙 2》, its artwork, HUD symbols, and trademarks belong to their
respective rights holders, including Axlebolt. SO2 Clipper is an independent
community tool and is not affiliated with or endorsed by Axlebolt.

The small matching templates in `scripts/assets/` and the generated runtime
snapshot are extracts of HUD symbols used for local visual recognition. The
gameplay images and animation in `docs/images/` come from the maintainer's
recordings and have player names and unrelated panels concealed. These game
materials are excluded from the project's MIT grant; the repository does not
grant rights to the underlying game assets. The game's publisher and official
game information are available at [Standoff 2](https://standoff2.com/); consult
the terms applicable to the game and to the particular material you use.

The README hero is an AI-generated conceptual illustration. It is not an
application screenshot or evidence of recognition accuracy. Gameplay examples
and workflow diagrams are labeled separately.
