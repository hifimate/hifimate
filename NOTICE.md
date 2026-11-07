# 第三方说明 / Third-party notices

HiFiMate 自身的代码按 MIT 许可证发布（见 `LICENSE`）。仓库里不包含其他项目的源码或二进制；
下面这些由安装器在安装时从上游取得，或在运行时作为独立程序使用，各自遵循自己的许可证：

| 组件 | 用法 | 许可证 |
|---|---|---|
| [CamillaDSP](https://github.com/HEnquist/camilladsp) 4.1.3 | 安装器下载官方发布版，未修改 | GPL-3.0 / MPL-2.0 |
| [Music Assistant](https://github.com/music-assistant/server) | 经 [ma_custom_loader](https://github.com/neqq3/ma_custom_loader)（neqq3）打包的 Custom Loader 镜像，运行时拉取 | Apache-2.0 |
| [ma-provider-subscriber](https://github.com/neqq3/ma_custom_loader)（neqq3） | Docker 镜像，运行时拉取 | Apache-2.0 |
| PipeWire、WirePlumber、BlueZ、MPD、upmpdcli、Shairport Sync、Avahi、Samba | 发行版软件包，独立进程 | 各自许可证 |
| websocket-client、PyYAML、dbus-next、NumPy | 发行版软件包 | 各自许可证 |
| ncm-api、migu-api-enhanced、MA provider subscriber 及其 provider | Docker 镜像 / 运行时下载 | 各自许可证 |

- 频谱的显示效果参照了 [audioMotion-analyzer](https://github.com/hvianna/audioMotion-analyzer)（AGPL-3.0）公开的
  行为：1/24 倍频程分带、Bark/Mel 频率刻度公式、线性幅度的 boost 映射和 LED 外观。代码是本项目独立编写的，
  没有复制其源码；在此致谢。
- 房间校正中 iPhone 15 Pro Max、16 Pro、17 Pro、18 Pro 麦克风的补偿曲线，来自 Faber Acoustical 公开发表的测量图
  （见 `hifimate/mics/*.json` 的 `source` 字段和 README 的「房间校正」一节），经数字化处理，版权归原作者。
- Bilibili、咪咕、网易云音乐等名称和图标是其各自所有者的商标，仅用于标识对应服务。
