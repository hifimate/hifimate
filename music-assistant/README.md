# Music Assistant 使用说明

[Music Assistant](https://www.music-assistant.io/)（下文简称 MA）管理本地曲库和流媒体音乐源，用网页或手机播放、排队。

HiFiMate 部署的是 [neqq3](https://github.com/neqq3) 维护的 [Music Assistant Custom Loader](https://github.com/neqq3/ma_custom_loader)
（官方 MA 2.10.9 镜像加上启动时加载自定义音乐源的能力，不修改 MA 本身），以及同一项目的音乐源订阅器。感谢 Music Assistant 团队和 neqq3。

HiFiMate 默认一起部署它，并做了这些整合：

- 用 HiFiMate 的播放器播放时，声音按原始采样率送到 DAC，和其他输入一样参与自动切换；
- 自带 B 站和咪咕两个音乐源；
- MA 网页右下角加了 HiFiMate 的频谱浮窗，显示正在播放和歌词；
- HiFiMate 控制页可以显示和控制 MA 的播放队列（曲目、进度、上下首、拖动）。

网页地址：`http://<主机IP>/`。用安装时创建的管理员账户登录，浏览器登录一次后 90 天内不用再登录。

## 首次设置

安装器已经替你做好了播放器的部分：把 HiFiMate 添加为 MA 的播放器（名为 `<名称> MA`），并设好无损输出、DAC 支持的全部采样率、
按原始采样率播放和音量控制。你只需要告诉 MA 音乐在哪里：

### 1. 添加本地音乐

主机上的音乐库 `/srv/hifimate/music`（安装选项 `--music-directory` 可改）在 MA 里是 `/media`，只读。往里放歌见 [README](../README.md#往音乐库里放歌)。

1. Settings → Providers → Add，选 **Filesystem (local disk)**。
2. 路径填 `/media`，保存。MA 开始扫描曲库。

### 2. 添加流媒体音乐源（可选）

在同一个 Add 列表里添加 QQ 音乐、网易云音乐、Spotify 等，以及 HiFiMate 自带的 **Bilibili** 和 **Migu Music**，见下文[自带的音乐源](#自带的音乐源)。

然后在播放器列表里选 `<名称> MA` 播放。

### 为什么播放要选 `<名称> MA`

MA 也会在列表里显示 `<名称> AirPlay`，那是 HiFiMate 的 AirPlay 输入，但 AirPlay 最高只有 44.1 kHz/16 bit。
`<名称> MA` 走 HiFiMate 内部专用的连接，能按曲目的原始采样率播放。

### 安装器设好的播放器设置

在这个播放器的设置里可以看到下面这些值。你自己改过的设置，重新安装时不会被改回来；
在 MA 里删除了这个播放器的 provider（MPD Players），重新安装也不会再加回来。

| 设置 | 值 | 原因 |
|---|---|---|
| Output codec | WAV | MA 默认的 MP3 是有损的 |
| Supported sample rates | DAC 支持的全部采样率，16 和 24 bit | MA 默认只有 44.1/48 kHz、16 bit，高采样率的曲目会被降采样 |
| Flow mode sample rate | bit perfect | 按曲目的原始采样率输出 |
| Volume Control | Native | MA 的音量直接控制这一路，100% 为原始音量 |
| 队列的 Volume normalization | Disabled | 不让 MA 自动改变音量 |

另外建议在播放器设置里关闭 DSP、在队列设置里关闭 Crossfade，用 HiFiMate 的 EQ 和房间校正。

安装后换了 DAC 时，重新运行一次 `hifimate install` 不会改动已有的采样率设置（它们可能是你选的）；需要时在这里手动勾选。
勾选了 DAC 不支持的采样率时，播放这种曲目会没有声音（HiFiMate 不会偷偷重采样），控制页会显示原因。

MA 支持的采样率是 44.1、48、88.2、96、176.4、192、352.8、384 kHz。这个播放器每次换采样率时，播放会有一个短暂的停顿，
这是 MA 重新建立音频流造成的。

## 播放

在 MA 网页或 MA 手机 App 里选择 `<名称> MA` 播放。播放后，HiFiMate 控制页和 MA 网页的频谱浮窗都会显示曲目、进度和歌词。

MA 正在播放时，从手机用 AirPlay、DLNA 或蓝牙开始播放，会切换到手机的声音，MA 自动暂停；反过来也一样。

## 自带的音乐源

两个音乐源都是可选的，在 Settings → Providers → Add 里添加后才会启用。不想安装时，安装时加
`--without-ma-provider bilibili`、`--without-ma-provider migu`，或用 `--without-ma-providers` 两个都不装。

- **Bilibili**：播放 B 站视频的音轨，支持搜索、UP 主投稿、多 P 视频、收藏夹和音乐榜单。可以扫码登录自己的账号，以便访问收藏夹。
- **Migu Music（咪咕音乐）**：通过主机上的咪咕接口服务访问咪咕曲库，支持搜索、专辑、歌手、歌单、榜单和歌词。可以登录自己的账号。

能播放哪些内容取决于你自己账号的权益，见 [README 的免责声明](../README.md#免责声明--disclaimer)。

## 歌词

歌词由主机上的网易云音乐接口服务（`hifimate-ncm-api`）和咪咕接口服务（`hifimate-migu-api`）提供，不需要添加对应的音乐源。来自咪咕的曲目直接用咪咕歌词；其他曲目先在网易云按歌名、歌手和时长匹配，找不到再查咪咕。
匹配不上时宁可不显示，控制页会显示原因。这两个服务只监听主机内部地址，不对局域网开放。

## 第三方音乐源

MA 社区还有很多第三方音乐源。HiFiMate 部署了一个订阅器（`hifimate-ma-provider-subscriber`），可以自动从 GitHub 下载并更新它们。
默认不订阅任何仓库。

第三方音乐源不属于 HiFiMate，它们的代码会在 MA 里运行，能访问 MA 的全部数据。添加前请自己审核代码，并确认使用它符合所在地法律和平台条款。

添加方法：

1. 打开 HiFiMate 的设置页面（控制页右上角的齿轮，`http://<主机IP>/ctrl/settings/`），用控制页密码登录。
2. 在「第三方音乐源订阅」里输入仓库（`owner/repo`，或 GitHub、Gitee、GitCode 的仓库地址），点「添加」，再点「保存并同步」。
   页面会让订阅器下载这些仓库，然后重启 MA 加载它们，大约 1 分钟。同步时出现的错误会显示在控制页的事件里。
3. 在 MA 的 Settings → Providers → Add 里添加新的音乐源。

页面上也能看到每个仓库的版本、最后同步时间和它装了哪些音乐源；删除仓库时，可以选择同时删除它装的音乐源。
之后订阅器每 6 小时自动检查一次更新。设置保存在 `/opt/hifimate/music-assistant/provider-subscriber/options.json`，也可以手动编辑。

## 更换网页端口

主机的 80 端口被别的网页服务占用时：

1. 编辑 `/opt/hifimate/music-assistant/.env`（`sudo nano …`），把 `MUSIC_ASSISTANT_HTTP_PORT=80` 改为例如 `8080`。
2. 应用修改：

   ```sh
   cd /opt/hifimate/music-assistant
   sudo docker compose up -d --wait
   ```

之后 MA 在 `http://<主机IP>:8080/`，控制页在 `http://<主机IP>:8080/ctrl/`。房间校正用的 HTTPS 端口同理，是 `MUSIC_ASSISTANT_HTTPS_PORT`。

`.env` 属于你，重新安装和升级都不会覆盖它。

## 数据和备份

- MA 的数据（账户、曲库索引、设置）在 `/var/lib/hifimate/music-assistant/`。
- 每次重新安装前，安装器会把 MA 的部署配置（`compose.yml`、`.env`、网页配置）备份到数据目录下的
  `.hifimate-deployment-backups/`。这不是数据备份。

备份 MA 数据时先停止它，复制整个数据目录，再启动：

```sh
cd /opt/hifimate/music-assistant
sudo docker compose stop server
sudo cp -a /var/lib/hifimate/music-assistant /root/ma-backup-$(date +%F)
sudo docker compose start server
```

## 升级 MA

HiFiMate 使用经过验证的 MA 2.10.9。镜像版本写在 `.env` 里，重新安装和 `hifimate upgrade` 都不会改动它。
想自己换版本时（兼容性需要自己确认）：

1. 停止播放，按上一节备份数据。
2. 编辑 `/opt/hifimate/music-assistant/.env` 里的 `MUSIC_ASSISTANT_IMAGE`（`sudo nano …`）。
3. 拉取并重启：

   ```sh
   cd /opt/hifimate/music-assistant
   sudo docker compose pull
   sudo docker compose up -d --wait
   sudo docker compose ps
   ```

要回到原来的版本，把 `.env` 改回去，恢复备份的数据目录，再执行第 3 步。

## 这些容器各是什么

| 容器 | 作用 |
|---|---|
| `hifimate-music-assistant` | MA 本身（官方 2.10.9 镜像加一个加载自定义音乐源的启动脚本） |
| `hifimate-music-assistant-web` | 网页入口（nginx）：MA 网页、控制页、频谱、房间校正 |
| `hifimate-ncm-api` | 网易云音乐接口，用于歌词 |
| `hifimate-migu-api` | 咪咕音乐接口，用于咪咕音乐源和歌词；安装时在本机构建 |
| `hifimate-ma-provider-subscriber` | 第三方音乐源订阅器 |

`hifimate status` 会显示它们的状态。技术细节见 [ARCHITECTURE.md](../docs/ARCHITECTURE.md#7-music-assistant-整合)。
