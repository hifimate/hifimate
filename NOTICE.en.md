[中文](NOTICE.md) | **English**

# Third-party notices

HiFiMate's own code is released under the MIT license (see `LICENSE`). The repository contains no source code or binaries of other projects;
the components below are obtained from upstream by the installer at install time, or used at run time as separate programs, and each follows its own license:

| Component | Usage | License |
|---|---|---|
| [CamillaDSP](https://github.com/HEnquist/camilladsp) 4.1.3 | The installer downloads the official release, unmodified | GPL-3.0 / MPL-2.0 |
| [Music Assistant](https://github.com/music-assistant/server) | Custom Loader image packaged by [ma_custom_loader](https://github.com/neqq3/ma_custom_loader) (neqq3), pulled at run time | Apache-2.0 |
| [ma-provider-subscriber](https://github.com/neqq3/ma_custom_loader) (neqq3) | Docker image, pulled at run time | Apache-2.0 |
| PipeWire, WirePlumber, BlueZ, MPD, upmpdcli, Shairport Sync, Avahi, Samba | Distribution packages, separate processes | Their respective licenses |
| websocket-client, PyYAML, dbus-next, NumPy | Distribution packages | Their respective licenses |
| ncm-api, migu-api-enhanced, MA provider subscriber and its providers | Docker images / downloaded at run time | Their respective licenses |

- The look of the spectrum display follows the publicly documented behavior of [audioMotion-analyzer](https://github.com/hvianna/audioMotion-analyzer) (AGPL-3.0):
  the 1/24-octave banding, the Bark/Mel frequency scale formulas, the boost mapping of linear amplitude and the LED appearance. The code was written independently for this project,
  without copying its source; our thanks to its author.
- The compensation curves for the iPhone 15 Pro Max, 16 Pro, 17 Pro and 18 Pro microphones used in room correction come from measurement plots publicly published by Faber Acoustical
  (see the `source` field of `hifimate/mics/*.json` and the "Room correction" section (「房间校正」) of the README); they were digitized, and the copyright belongs to the original authors.
- Names and icons such as Bilibili, Migu (咪咕) and NetEase Cloud Music (网易云音乐) are trademarks of their respective owners and are used only to identify the corresponding services.
