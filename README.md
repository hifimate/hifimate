# HiFiMate

把一台 Linux 小主机和一台 DAC 变成家里的高保真播放器：全家人的手机、电脑都能直接把音乐投过来，声音按原始采样率出声。

## 什么时候用 HiFiMate

你有一套音箱（或耳放）和一台 USB DAC，还有一台闲置的电脑，比如迷你主机、旧笔记本、NUC。遇到下面这些情况，HiFiMate 就有用：

- **家里每个人用的设备不一样。** iPhone 想用 AirPlay，安卓想用蓝牙或 DLNA，电脑上开着音乐 App。
  你不想每次换人就拔插线、切换 DAC 的输入。
- **你在意音质。** 手里有 FLAC、高解析度（96 kHz、192 kHz 甚至更高）的曲库，希望它们按原始采样率送到 DAC，
  而不是被系统统一转成 48 kHz。
- **你想用手机遥控本地曲库。** 歌放在硬盘里，希望像用流媒体 App 一样在手机上搜索、排队、播放。
- **音箱在房间里低频轰头，或者你想给音箱加 EQ。** 但不想研究 DSP 软件的配置。
- **你想看到正在播放什么。** 曲目、滚动歌词、频谱，在手机或电脑的浏览器里就能看。

## 它能帮你做什么

装好以后，HiFiMate 在后台一直运行，不需要显示器和键盘：

- **同时等待四种输入**：AirPlay、DLNA、蓝牙（主机就是一个蓝牙音箱）、Music Assistant。任何人在自己的设备上按播放就能出声；
  后按播放的人接管，前一台设备会被自动暂停，不会两路声音混在一起。
- **自动跟随采样率**：每首歌是多少采样率，DAC 就切换到多少（44.1 kHz 到 768 kHz），不做重采样。
  DAC 不支持的格式会明确报错，不会悄悄降质播放。
- **一个网页管理一切**：浏览器打开控制页，可以看正在播放的曲目、逐字滚动的歌词和实时频谱，播放/暂停、切歌、拖动进度，
  切换输入和 DAC，给新手机配对蓝牙，查看服务状态。
- **管理你的曲库和流媒体**：内置 Music Assistant（一个加了自定义音乐源加载能力的版本，见[下文](#music-assistant)），
  用网页或手机 App 浏览本地曲库、排播放队列，接入 QQ 音乐、网易云音乐、B 站、咪咕、Spotify、Tidal 等音乐源。
- **改善房间的声音**：拿 iPhone 在听音位置测几次，自动算出削掉房间低频共振的 EQ，一键应用、随时关闭。
  iPhone 15 及以后的机型都内置了麦克风补偿曲线。
- **加你自己的 EQ**：按 CamillaDSP 的写法写几行滤波器，所有采样率通用，改完立即生效。
- **省心**：开始播放时淡入，不会突然爆音；5 分钟没有声音就释放 DAC；一条命令安装，`hifimate upgrade` 升级；卸载时把服务、配置和改过的系统设置都还原。

## 不适合的情况

- 多房间同步播放：HiFiMate 只驱动一台 DAC。
- 主机是 Windows 或 macOS：HiFiMate 只能装在 Linux 上（手机和电脑作为发送端不限系统）。
- 通过 HDMI、光纤或同轴输出：目前只支持 USB DAC 和主机自带的模拟输出。

## 需要什么

**主机**：一台 Linux 电脑，CPU 为 x86_64 或 aarch64，系统为 Debian 13、Ubuntu 26.04、Fedora 43、Arch 或 openSUSE Tumbleweed 及更新版本
（Ubuntu 24.04 需要先升级 PipeWire，见 [INSTALL.md](INSTALL.md#11-操作系统)）。它需要：

- **一个 USB 口**接 DAC。没有 DAC 时也可以用主机自带的模拟输出（耳机口）。
- **网络**：有线网口最好；用 Wi‑Fi 时尽量连 5 GHz。手机、电脑要和主机在同一个局域网。
- **蓝牙**（要用蓝牙输入时）：主机内置或 USB 蓝牙适配器都可以。
- 使用 Music Assistant 时还需要 Docker，内存建议 2 GB 以上。
- 一个能用 sudo 的账号，用来安装。

迷你主机、NUC、旧笔记本都很合适。**树莓派**也能用（建议 4 或 5，系统用基于 Debian 13 的 64 位 Raspberry Pi OS），但要注意：

- 树莓派板载的 Wi‑Fi 和蓝牙是同一颗芯片、共用一根天线。Wi‑Fi 在 2.4 GHz 上传输数据（比如 AirPlay、Music Assistant 的音频流）时，
  蓝牙音频容易断续。用网线连接，或者 Wi‑Fi 只连 5 GHz；要用蓝牙输入时，最好再插一个 USB 蓝牙适配器。
- USB DAC 的供电要足：用官方电源，DAC 功耗大时加有源 USB 集线器。
- 用 SD 卡做系统盘时，Music Assistant 的曲库扫描和数据库写入会比较慢，SD 卡也容易损坏；能用 USB SSD 启动更好。

## 快速开始

1. 在主机上运行（会请求 sudo 密码）：

   ```sh
   curl -fsSL https://github.com/hifimate/hifimate/releases/latest/download/install.sh | sudo sh
   ```

   安装器会装好所需的系统软件包，创建一个专门运行 HiFiMate 的用户 `hifimate`，然后依次询问：
   - 设备名称，例如 `客厅`，手机上会看到 `客厅 AirPlay`、`客厅 DLNA`、`客厅 Bluetooth`；
   - 一个密码：它是 `hifimate` 用户的 Linux 密码，控制页和音乐共享都用它，HiFiMate 自己不另存密码；
   - Music Assistant 的管理员账户。

   装好后开机自动运行，不需要有人登录。
2. 在手机上播放：
   - iPhone：控制中心 → 音乐卡片右上角的 AirPlay 图标 → 选 `客厅 AirPlay`。
   - 安卓：用支持 DLNA 的播放器（如 BubbleUPnP）选 `客厅 DLNA`，或者用蓝牙连接 `客厅 Bluetooth`（先配对，见下文）。
3. 在浏览器打开 `http://<主机IP>/ctrl/` 查看控制页，`http://<主机IP>/` 是 Music Assistant。
4. 把歌放进音乐库：在电脑上打开共享文件夹 `\\<主机IP>\Music`（见下文[往音乐库里放歌](#往音乐库里放歌)）。

不需要 Music Assistant 或共享文件夹时，把命令最后的 `| sudo sh` 换成
`| sudo sh -s -- --without-music-assistant --without-share`。更多安装选项、无人值守安装和前置条件见 [INSTALL.md](INSTALL.md)。

## 使用

### AirPlay

iPhone、iPad 在控制中心的播放卡片里点 AirPlay 图标，选 `<名称> AirPlay`；Mac 在菜单栏「控制中心 → 声音」里选同名设备。
在发送端暂停后，再从其他输入播放即可接管；从控制页暂停或切走时，HiFiMate 会让发送端一起暂停。

### DLNA

在支持 DLNA/UPnP 输出的播放器（BubbleUPnP、Foobar2000、各类音乐 App 的「投放」）里选 `<名称> DLNA`。

### 蓝牙

主机就像一个蓝牙音箱。第一次连接前先打开配对窗口（120 秒），可以在控制页的蓝牙卡片里点「配对 120 s」，或在主机上运行：

```sh
sudo hifimate pair
```

然后在手机的蓝牙设置里选 `<名称> Bluetooth`。配对成功后窗口自动关闭，以后手机直接连接即可。
蓝牙的音质取决于手机和蓝牙编码，通常达不到 AirPlay、DLNA 的采样率。

### Music Assistant

HiFiMate 使用的不是 Music Assistant 原版，而是 [neqq3](https://github.com/neqq3) 维护的
[Music Assistant Custom Loader](https://github.com/neqq3/ma_custom_loader)：它基于官方 Music Assistant 2.10.9 镜像，
只增加了启动时加载自定义音乐源的能力，不修改 MA 本身的代码。感谢 [Music Assistant](https://www.music-assistant.io/) 团队和 neqq3 的工作。

可以使用的音乐源：

- **Music Assistant 官方支持的**（在 MA 的设置里直接添加）：本地文件、SMB/NFS 网络共享、WebDAV；Plex、Jellyfin、Emby、Subsonic 等媒体服务器；
  **QQ 音乐**、**网易云音乐**、Spotify、Apple Music、Tidal、Qobuz、Deezer、YouTube Music、SoundCloud、Bandcamp；
  TuneIn、Radio Browser 等网络电台，以及播客、有声书。完整列表见 [MA 官方文档](https://www.music-assistant.io/music-providers/)。
- **HiFiMate 自带的**：Bilibili（B 站视频的音轨）和咪咕音乐。
- **可以订阅的第三方音乐源**（默认不安装，需要自己添加订阅，见 [Music Assistant 使用说明](music-assistant/README.md#第三方音乐源)）：
  例如 [ma_ncloud_music](https://github.com/neqq3/ma_ncloud_music)（另一个网易云音乐源）、
  [music-assistant-providers](https://github.com/andychao2024/music-assistant-providers)（洛雪音乐、GD 音乐台、豆瓣和网易云的歌手/专辑资料补全等）。
  它们由各自的作者维护，不属于 HiFiMate。

能播放哪些内容取决于你自己账号的权益，见[免责声明](#免责声明--disclaimer)。

打开 `http://<主机IP>/`，用安装时创建的管理员登录。安装器已经把 HiFiMate 设为 MA 的播放器（`<名称> MA`）；你只需添加音乐源，就可以选它播放。
具体步骤见 [Music Assistant 使用说明](music-assistant/README.md)。

### 往音乐库里放歌

主机上的音乐库是 `/srv/hifimate/music`，安装器把它共享到局域网，名为 `Music`：

- **Windows**：在资源管理器的地址栏输入 `\\<主机IP>\Music`。
- **Mac**：Finder →「前往」→「连接服务器」，输入 `smb://<主机IP>/Music`。Finder 的侧边栏「网络」里也能看到这台主机。
- **Linux**：文件管理器里打开 `smb://<主机IP>/Music`。

用户名 `hifimate`，密码是控制页密码。把歌曲文件夹复制进去，Music Assistant 会定期扫描曲库；想马上看到新歌，在 MA 的音乐源设置里手动同步。
支持 FLAC、ALAC、WAV、AIFF、MP3、AAC 等常见格式，建议按「歌手/专辑/曲目」整理。

### 控制页

`http://<主机IP>/ctrl/`。不登录可以查看所有状态；播放控制、切换输入/输出、蓝牙配对、重启服务需要用控制页密码登录。
电脑上显示信号图和频谱，手机上分成「总览」「服务」「日志」「频谱」四页。

Music Assistant 页面右下角也有一个可拖动的频谱浮窗，显示正在播放的曲目和歌词；它可以显示 HiFiMate 的输出，
也可以分析浏览器自己正在播放的声音。

### 房间校正

需要一部 iPhone，约 4 分钟：

1. 第一次使用要信任 HiFiMate 的本地证书（iPhone 只允许 HTTPS 网页使用麦克风）：在 iPhone 上打开
   `http://<主机IP>/roomfit`，按页面提示下载并安装「HiFiMate Local CA」，再到「设置 → 通用 → 关于本机 → 证书信任设置」里打开完全信任。
2. 用 HTTPS 打开测量页，用控制页密码登录，选择手机型号。
3. 摘掉手机壳，保持平时听音的音量，按提示把手机放在听音位置（耳朵高度，底部麦克风朝向音箱）。测量会播放扫频声。
4. 依次测中心和前后左右约 30 cm 的几个位置（至少 3 个），页面会说明每次测量好不好用。
5. 查看校正前后的预测曲线，点「应用」。之后可以在控制页的 DSP 卡片里随时开关。

校正只处理 35–350 Hz 的房间共振，只削峰、不补谷。

手机麦克风本身低频不平直，测量时会扣除所选机型的麦克风曲线。这些曲线来自 [Faber Acoustical](https://www.faberacoustical.com/)
（SignalScope 的开发者）在博客上公开发表的消声室实测，数据权利归 Faber Acoustical 所有：

| 实测机型 | 原文 | 同代沿用此曲线的机型 |
|---|---|---|
| iPhone 15 Pro Max | [2024-09](https://www.faberacoustical.com/blog/2024/ios/iphone/measured-iphone-15-pro-max-microphone-frequency-response-and-directivity/) | iPhone 15、15 Plus、15 Pro |
| iPhone 16 Pro | [2024-09](https://www.faberacoustical.com/blog/2024/ios/iphone/measured-iphone-16-pro-microphone-frequency-response-and-directivity/) | iPhone 16、16 Plus、16 Pro Max、16e |
| iPhone 17 Pro | [2025-09](https://www.faberacoustical.com/blog/2025/ios/iphone/measured-iphone-17-pro-microphone-frequency-response-and-directivity/) | iPhone 17、Air、17 Pro Max、17e |
| iPhone 18 Pro | [2026-10](https://www.faberacoustical.com/blog/2026/ios/iphone/measured-iphone-18-pro-microphone-frequency-response-and-directivity/) | iPhone 18 Pro Max、iPhone Duo |

没有单独实测的机型沿用同代 Pro 的曲线，页面上标为「参考 … 实测」。

### 自己的 EQ

在控制页 DSP 卡片里点「我的 EQ」，或直接打开 `http://<主机IP>/ctrl/eq/`，用控制页密码登录：

- 在曲线上拖动圆点改频率和增益，滚轮或双指缩放改 Q，双击空白处添加滤波器；也可以在下面的列表里直接输入数值。
- 曲线由主机上的 CamillaDSP 实际计算，左右声道分开显示，还能叠加房间校正和两者合计；提升超过预留余量、可能削波时会提醒。
- 可以粘贴 REW、AutoEQ、Equalizer APO 导出的滤波器列表直接导入。
- 点「应用」立即生效，不中断播放；「旁路对比」临时关掉 EQ 做 A/B 对比；每次应用前的内容保留为历史版本（最近 10 个），可以随时载入。

FIR（Conv）、DiffEq 等列表编辑不了的类型，在页面的「文本」模式里写。EQ 保存在 `/opt/hifimate/config/dsp-filters.yml`，
写法和 CamillaDSP 配置的 `filters`、`pipeline` 两段相同，也可以在主机上直接编辑，例如：

```yaml
filters:
  bass_peak: {type: Biquad, parameters: {type: Peaking, freq: 52, gain: -3.0, q: 1.7}}
  air_shelf: {type: Biquad, parameters: {type: Highshelf, freq: 8000, gain: 2.0, q: 0.7}}
pipeline:
  - {type: Filter, channels: [0, 1], names: [bass_peak, air_shelf]}
```

在主机上改完运行 `sudo hifimate reload-filters`：它先检查写法，正在播放时立即生效。
这个文件属于你，重新安装和升级都不会改动它。可用的滤波器类型和规则见 [INSTALL.md](INSTALL.md#43-自己的-eq)。

### 设置

控制页右上角的齿轮（`http://<主机IP>/ctrl/settings/`）可以修改：淡入时长、没有声音多久后释放 DAC、允许的采样率，
以及 Music Assistant 的第三方音乐源订阅。设备名称、密码和网页端口仍在主机上用命令修改，页面上列出了对应的命令。

## 管理

所有命令都用 sudo 运行：

| 命令 | 作用 |
|---|---|
| `sudo hifimate status` | 查看版本、输出设备、音乐库和所有服务的状态 |
| `sudo hifimate upgrade` | 升级到最新版本，沿用上次安装的选项 |
| `sudo hifimate install --name 书房` | 修改选项后重新安装（这里是改名称） |
| `sudo hifimate pair` | 打开蓝牙配对窗口 |
| `sudo hifimate reload-filters` | 检查并加载改过的 EQ |
| `sudo hifimate output` | 列出输出设备；`--choose` 选择并切换 |
| `sudo hifimate password` | 修改密码（`hifimate` 用户的 Linux 密码，控制页和音乐共享共用） |
| `sudo hifimate logs airplay` | 查看日志（可以写 `dlna`、`bluetooth`、`camilladsp` 等，不写则全部） |
| `sudo hifimate restart` | 重启所有服务 |
| `sudo hifimate uninstall` | 卸载；保留音乐库、Music Assistant 的数据和你的设置 |

## 安装后的文件

HiFiMate 以专门的用户 `hifimate` 运行，所有文件都归这个用户所有，不在任何人的主目录里。你可能会用到的：

| 内容 | 位置 | 说明 |
|---|---|---|
| 音乐库 | `/srv/hifimate/music` | 共享为 `\\<主机>\Music`，Music Assistant 里是 `/media` |
| 自己的 EQ | `/opt/hifimate/config/dsp-filters.yml` | EQ 页面编辑；手动改完运行 `sudo hifimate reload-filters` |
| EQ 的历史版本 | `/opt/hifimate/state/eq/versions/` | EQ 页面「应用」前的内容，保留最近 10 个 |
| 播放设置 | `/opt/hifimate/config/settings.json` | 设置页面写入：淡入时长、空闲释放时间、允许的采样率 |
| 其他设置 | `/opt/hifimate/config/hifimate.conf` | 安装器管理的默认值，不要手改，见 [INSTALL.md](INSTALL.md#46-其他配置) |
| 房间校正 | `/opt/hifimate/config/room-correction.json` | 由房间校正页面写入，不需要手改 |
| Music Assistant 的端口等 | `/opt/hifimate/music-assistant/.env` | 见 [Music Assistant 使用说明](music-assistant/README.md#更换网页端口) |
| 第三方音乐源订阅 | `/opt/hifimate/music-assistant/provider-subscriber/options.json` | 设置页面编辑，见 [Music Assistant 使用说明](music-assistant/README.md#第三方音乐源) |
| Music Assistant 的数据 | `/var/lib/hifimate/music-assistant/` | 账户、曲库索引、设置；备份时复制整个目录 |
| 本地证书 | `/opt/hifimate/config/tls/ca.crt` | 房间校正页面用的 HTTPS 证书的根证书 |
| 安装选项 | `/etc/hifimate/install.json` | `sudo hifimate upgrade` 和 `install` 沿用的选项，不含密码 |
| `hifimate` 命令 | `/usr/local/bin/hifimate` | |
| 日志 | 系统日志 | 用 `sudo hifimate logs` 查看 |

大部分可以在页面上改；要在主机上编辑这些文件，用 `sudo nano 文件名`（nano 保存时保留文件的所有者）。`/opt/hifimate` 下的其他文件是程序本身，升级时会被替换，不要手改。
重新安装、升级和卸载都会保留上表中的音乐库、EQ、房间校正、证书、`.env`、订阅设置和 Music Assistant 的数据。

## 文档

- [INSTALL.md](INSTALL.md)：前置条件、安装选项、设置、维护、卸载、故障排查，以及开发者说明。
- [music-assistant/README.md](music-assistant/README.md)：Music Assistant 的设置、音乐源和升级。
- [ARCHITECTURE.md](docs/ARCHITECTURE.md)：工作原理和技术细节。

## 免责声明 / Disclaimer

HiFiMate 是个人开源项目，与网易云音乐、哔哩哔哩、咪咕音乐、Music Assistant 及任何硬件厂商均无关联。
HiFiMate 随附的 Bilibili、咪咕音乐源只用你自己的账号、按平台公开的接口访问内容，不提供、不破解任何付费、会员或版权保护内容；
能播放什么取决于你账号本身的权益。通过 Music Assistant 另行安装的第三方音乐源不属于 HiFiMate，其行为由各自作者负责。
请遵守所在地法律和各平台的服务条款，内容的版权归权利人所有，使用任何音乐源的责任由使用者自行承担。

HiFiMate is an independent open-source project, not affiliated with NetEase Cloud Music, Bilibili, Migu,
Music Assistant or any hardware vendor. The bundled Bilibili and Migu providers only access content with your own
account through each service's public interfaces; nothing paid, premium or protected is provided or circumvented.
Third-party providers installed through Music Assistant are not part of HiFiMate.
You are responsible for complying with local law and each service's terms.

## 许可证

MIT，见 [LICENSE](LICENSE)，按「原样」提供，不附带任何担保。使用的第三方软件和数据见 [NOTICE.md](NOTICE.md)。
