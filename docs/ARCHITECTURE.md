# 架构与实现

本文说明 HiFiMate 的工作原理和实现细节，面向想了解内部机制或参与开发的读者。使用方法见 [README](../README.md)，
安装和维护见 [INSTALL.md](../INSTALL.md)。

## 1. 总览

```text
       DLNA 控制端 → upmpdcli → MPD :6600 ──→ dlna_in ─────────────────┐
Music Assistant → MPD Players → MPD :6601 ──→ music_assistant_dlna_in ┤
       AirPlay 发送端 → Shairport Sync ──→ airplay_in ─────────────────┤
            手机 → BlueZ & WirePlumber ──→ bluez_input.* ──────────────┤
                                                                      ↓
                                                                仲裁服务选出的一路 → CamillaDSP → ALSA hw → DAC
```

设计原则：

- **复用成熟的软件**：协议接收用 Shairport Sync、upmpdcli、MPD、BlueZ，音频图用 PipeWire，DSP 用官方 CamillaDSP，
  曲库用 Music Assistant。HiFiMate 自己的代码只做协调：决定谁在播放、配置采样率、提供控制页和频谱。
- **不混音**：每路输入是一个独立的 PipeWire 节点（lane），DSP 只采集当前获准的那一路。
- **只认明确的播放操作**：播放权来自协议里的「开始播放」事件，不从「有数据」「已连接」推测。
- **宁可静音也不出错声**：采样率、设备能力、链路回读任何一项对不上，就静音并报告，不偷偷重采样或降级。
- **不改系统**：所有服务是专用用户 `hifimate` 的 systemd 用户服务；需要 root 的设置（实时调度上限、蓝牙音箱类别、Samba 共享）只在安装时做，有归属记录，卸载时还原。

## 2. 组成部分

### 2.1 用户服务

所有服务都是运行用户（系统安装时是 `hifimate`）的 systemd 用户服务；音频图是这个用户自己的 PipeWire 和 WirePlumber。

| 服务 | 作用 |
|---|---|
| `hifimate.service` | 仲裁服务（`format_daemon.py`）：接收各协议的播放事件，决定播放权，配置采样率和 DSP |
| `hifimate-camilladsp.service` | CamillaDSP，`--wait --no_config` 启动，获准前不打开声卡 |
| `hifimate-mpd.service` | DLNA 用的 MPD（127.0.0.1:6600） |
| `hifimate-music-mpd.service` | Music Assistant 用的 MPD（127.0.0.1:6601） |
| `hifimate-dlna.service` | upmpdcli，对局域网发布 DLNA renderer，控制 MPD 6600 |
| `hifimate-airplay.service` | Shairport Sync |
| `hifimate-airplay-net.service` | 跟随默认路由网卡，变化时重启 Shairport（见 5.4） |
| `hifimate-bluetooth.service` | 维护蓝牙适配器名称，关闭配对窗口之外的可发现状态 |
| `hifimate-spectrum.service` | 频谱服务（127.0.0.1:5008），见 [SPECTRUM.md](SPECTRUM.md) |
| `hifimate-ctrl.service` | 控制页（127.0.0.1:5009），见第 9 节 |

CamillaDSP、两路 MPD、Shairport 通过 `bin/hifimate-rt` 启动，可选地申请实时调度（第 13 节）。

### 2.2 代码模块

| 模块 | 职责 |
|---|---|
| `receiver_events.py` | 收集各协议的播放事件，绑定唯一的接收流，会话隔离（fencing） |
| `mpd.py` | 订阅两路 MPD 的 `idle player`，产生播放/停止边沿 |
| `airplay.py` | 解析 Shairport 的 metadata 管道，产生 AirPlay 会话 |
| `bluetooth.py` | 监听 BlueZ MediaPlayer 的状态边沿 |
| `formats.py` | 格式事务：准备候选、核对、提交、淡入、失败静音 |
| `remote_pause.py` | 让被切走的发送端暂停，并回读结果 |
| `pw_events.py` | 过滤 PipeWire 事件，忽略探测工具自身产生的变化 |
| `format_daemon.py` | 本地 IPC、订阅生命周期、工作队列、systemd 就绪通知 |
| `runtime_paths.py` | 运行目录、锁、socket、FIFO 的归属检查和崩溃恢复 |
| `output_devices.py` | 识别可用的 USB 和本机模拟输出，切换输出的事务 |
| `identity.py`、`bluetooth_alias.py`、`bluetooth_pair.py` | 设备名称、蓝牙名称的设置与还原、配对窗口 |
| `airplay_net.py` | 按默认路由选择 Shairport 广播的网卡 |
| `spectrum.py`、`dsp_model.py`、`spectrum_proxy.py` | 频谱 |
| `ctrl.py`、`ctrl_state.py`、`ctrl_auth.py`、`nowplaying.py`、`ma_api.py`、`lyrics.py` | 控制页、正在播放、歌词 |
| `roomfit.py`、`room_eq.py`、`tls.py` | 房间测量、校正滤波器、本地证书 |
| `user_filters.py` | 个人滤波器文件的检查和合并 |
| `realtime.py` | 启动前申请实时调度的小包装 |
| `cli.py` | `hifimate` 命令 |
| `core.py` | 播放权策略的纯内存模型，供测试使用 |

### 2.3 端口

| 端口 | 监听 | 用途 |
|---|---|---|
| 80、443 | 所有网卡 | nginx：MA 网页、`/ctrl/`、`/_hifimate/`、房间校正（HTTPS） |
| 8095、8097 | 所有网卡 | MA 网页接口、MA 音频流 |
| 6600、6601 | 127.0.0.1 | 两路 MPD |
| 5008 | 127.0.0.1 | 频谱 SSE 和前端文件 |
| 5009 | 127.0.0.1 | 控制页 |
| 3003、3004 | 127.0.0.1 | 网易云、咪咕接口服务 |
| 1234 | 127.0.0.1 | CamillaDSP websocket |

loopback 只防局域网直连，不是本机用户之间的权限边界。

## 3. 播放权

### 3.1 规则

- 最新一次有效的播放操作获得播放权，同一时刻只输出一路。
- 有效的播放操作是协议里的开始/恢复播放边沿：MPD 状态从非 play 变为 play；AirPlay 的 `pbeg`/`pres`；
  BlueZ MediaPlayer 的 Status 变为 playing。
- 连接设备、初始状态快照、重复的 playing、持续送来的 PCM 都不产生播放权。原来的发送端没能暂停、仍在送数据时，
  也不能凭这些抢回播放权。
- 当前一路停止后进入空闲，**不自动恢复**之前被切走的那一路；它需要一次新的播放操作。
- AirPlay 的 `prsm`/`pfls` 不抢占，唯一的例外是 HiFiMate 自己通过 DACP 暂停过的连接上的 `prsm`（用户在发送端恢复）。
  新连接（新的 `clip/daid/acre`）之后的 `pbeg` 总是新会话，以应对发送端或 Shairport 崩溃后没有 `pend` 的情况。
- DLNA 的播放权来自 MPD 的实际状态边沿；upmpdcli 的 hook 没有会话信息，只用来唤醒观察，不直接抢占或停止。

### 3.2 会话隔离

每次播放操作产生一个会话。接收线程先登记最新会话再入队，旧会话的事件（迟到的 stop、旧格式）不能改变新会话。
PipeWire 节点 ID 与 `object.serial` 绑定，Pulse index 另行解析；每个获准的 lane 必须恰好有一个活动的接收流，
同一 lane 有多个流、格式未知、serial 不匹配都会失败并静音。

### 3.3 让原来的发送端暂停

新的一路提交成功后，HiFiMate 独立地请求原来的一路暂停，并回读结果：

| 输入 | 暂停方式 | 回读 |
|---|---|---|
| DLNA、Music Assistant | MPD `pause 1` | MPD `status` |
| 蓝牙 | BlueZ `MediaPlayer1.Pause` | `Status` |
| AirPlay | DACP `pause`（凭据来自 Shairport 的 `daid/acre/clip`，按发送端 IP 和 DACP-ID 在 mDNS 里找服务） | DMAP 播放状态 |

结果记录为 `confirmed`（观察到已暂停/停止）、`accepted`（请求被接受但没确认）、`unsupported`（缺少远程控制信息或能力）、
`error`（请求或发现失败）、`stale`（会话已结束，未发送）。最近 32 条在 IPC 的 `remote_pause` 字段，也写入日志，不含 DACP token。
暂停失败不撤销新的播放权。DACP 凭据在 `pbeg` 时冻结，同一连接的 `pres` 复用；凭据不完整或 mDNS 不能唯一匹配时不猜。
不使用 `playpause`（可能把暂停变成播放），不跟随 HTTP 重定向。MPD 的 pause 只证明 renderer 暂停了，不证明控制 App 的界面已同步。

### 3.4 从控制页切换

控制页的「切换输入」在仲裁锁内重新检查目标 lane 是否有未 cork 的流（蓝牙检查 running 的 BlueZ source），使用接收线程最近保存的
设备/会话/远程控制信息，显式重新授权这个已退休的会话，然后走正常的候选验证和提交流程。没有数据或从未观察到播放请求的输入不能切换，
也不会尝试让发送端恢复播放。

## 4. 采样率跟随（格式事务）

### 4.1 步骤

1. 播放边沿产生候选（candidate），不立即改变当前播放者（owner）。
2. 读取 PipeWire 已协商的 `Format`，与 Pulse 接收流的 sample spec、目标 lane 相互核对。
3. 从输出设备的能力里取双声道 S32_LE 的离散采样率，生成 DSP 配置并 `ValidateConfigJson`。这时还没动旧的链路。
4. 交接推子（Aux4）静音，停止 DSP，并回读到 Inactive、活动配置为空（Stop 返回只表示已入队），最多等 3 秒，失败为 fatal。
5. 设置 DSP 的处理和采集采样率、ALSA S32_LE，关闭 resampler 和 rate adjust；有 FIR 时要求有这个采样率对应的滤波器。
6. 配置末尾加入 `_HiFiMateHandoff` Volume 滤波器（双声道、Aux4、最大 0 dB），静音回读通过后 `SetConfigJson`（它会自行启动处理）。
7. 核对：`pw-top` 中运行节点的 RATE、DSP 的配置和 Running 状态、交接推子、ALSA `hw_params`。
   虚拟节点的 `params.Format.rate` 可能一直显示 48 kHz，不能作为时钟证据。
8. 再检查一次源身份、格式和最新意图，才提交 owner、`ready`、`effective`。
9. 请求原来的一路暂停（3.3）。释放交接推子前再核对一遍源、图、DSP、DAC、会话和推子，然后开始淡入。

同一 owner 的格式变化产生新的 revision，重新走一遍；已经验证过的同格式刷新不会重启 DSP 或重新淡入。
旧 owner、旧 revision、旧 serial 都不能配置输出。

### 4.2 按组强制采样率

不使用全局的 `clock.force-rate`。`hifimate-camilladsp.service` 设置 `PULSE_PROP=node.force-rate=0`，DSP 的采集流每次启动时
只把它所在的图组（获准的 lane 和 DSP）强制到 `capture_samplerate`，其他 lane 保持各自的采样率。全局强制会让空闲的 lane 一起变，
例如手机的 A2DP 节点在 96 kHz 组里启动，再被切到 48 kHz 时，PipeWire 1.4 无法恢复（持续 XRun、输出全零）。

### 4.3 淡入

淡入用 CamillaDSP 原生的 Volume 滤波器逐采样插值：静音时是精确的零增益，释放时从 -100 dB 渐变到 0 dB，默认 400 ms
（`fade_ms`，20–2000）。主音量和 mute 始终由用户控制，淡入不改它们。停止或重建会取消正在进行的渐变，没有事后反向解静音的定时器。
状态 `fade` 为 `muted`、`ramping` 或 `released`，`released` 只表示配置的时长已过，不是响度测量。
Aux4 和 `_HiFiMateHandoff` 是保留的，自定义 DSP 不能使用或移除。

### 4.4 失败处理

失败时清除 `ready`、`effective`、`current`、`binding` 并停止输出，保留最后提交的 owner 和待准备的候选供诊断，不用旧配置自动恢复。
相同源身份和格式的失败不重复重配；新的格式证据或新的播放操作才重试。硬件恢复而输入没变化时，需要重新播放。
等待接收流默认 5 秒，格式回读重试窗口默认 3 秒，外部命令和 RPC 分别最多 5 秒、3 秒。

### 4.5 关于位深和 bit-perfect

DSP 内部是浮点，24 bit 的 PCM 用 S32_LE 硬件容器传给 DAC，容器位数不代表源文件的有效位深。
HiFiMate 跟随的是接收端**解码后的 PCM**：AirPlay（44.1 kHz/16 bit）、蓝牙或发送端已经降采样时，无法恢复原文件的格式。
经过 DSP（哪怕只有 Headroom）的输出不宣称 bit-perfect。

### 4.6 DSP 配置的组成

每份 DSP 配置（所有采样率）按这个顺序组成：预置 Headroom（`config/host-stack/camilladsp.yml`，随安装更新）→ 个人滤波器
（`config/dsp-filters.yml`，名称加 `_HiFiMateUser_` 前缀）→ 房间校正（`config/room-correction.json`）→ 交接推子。
Stop 清空活动配置后，下次准备优先用 CamillaDSP 的 `GetPreviousConfig`，没有时才读基础 YAML；个人滤波器和房间校正每次都按文件重新放入。
个人滤波器只允许 `Filter` 步骤和 Biquad、BiquadCombo、Gain、Delay、DiffEq、Conv（Volume、Loudness 会占用推子）。
文件有错误时照常播放但不带个人滤波器，`hifimate-ctrl reload-filters` 报告原因；正在播放时 reload 直接替换配置，不重开声卡。

## 5. 空闲、事件与恢复

### 5.1 空闲释放 DAC

owner 的流还在但没有声音时（发送端暂停后没有发出停止边沿、只送数字静音、或不再写数据），PipeWire 仍向 monitor 填零。
生成的 DSP 配置默认 `silence_threshold=-90 dB`、`silence_timeout=10 s`（用户自己的非零设置保留），CamillaDSP 因此进入 `Paused`：
暂停处理和 ALSA PCM，但设备仍打开，校验不会因此静音正在播放的会话。

仲裁只在有 owner 时每 5 秒读一次 `GetState`，不采集 PCM。连续 `Paused` 达到 `idle_timeout_s`（默认 300，0 关闭）后，先请求发送端暂停，
使以后的恢复一定产生新的播放边沿；结果为 confirmed、accepted 或 stale 时按停止处理：静音、Stop、关闭 ALSA、退休会话。
远程暂停 unsupported 或 error 时，恢复将无法观测，因此保持输出打开，同一会话不重试。

### 5.2 事件驱动

PipeWire 和 Pulse 的订阅驱动检查，重复的观察合并；空闲时不采集 PCM、不探测 DAC。
订阅 EOF、BlueZ 总线断开、队列溢出会让服务退出，交给 systemd 重启；未处理的异常和静音失败都记为 fatal 并以非零状态退出，
不会出现线程已死而服务仍显示正常的情况。启动时持有单实例锁并清理残留的 socket，复用自己的 FIFO inode 并排空旧消息；
启动快照不恢复之前的播放权。

MPD 不可用（启动、重启、崩溃循环）时以 0.25–5 秒退避重连，超过 15 秒在日志报告一次，恢复后再报告一次；连接被关闭时结束该路的会话，
重连后的状态按初始快照处理。命令超时和协议错误仍为 fatal。

### 5.3 服务依赖

DLNA、AirPlay 适配器对 `hifimate.service`、`hifimate-mpd.service` 只用 `Wants=` 加 `After=`：用硬依赖时，依赖启动失败会让 systemd
以 `dependency` 结果结束启动任务，而这种失败不会被 `Restart=` 重试。AirPlay 另以 `PartOf=hifimate.service` 跟随显式的 stop/restart，
以便重新打开重建的 metadata FIFO。两路 MPD 用 `Type=notify`，监听端口后才算启动。
upmpdcli 1.9.17 在启动完成前收到的 SIGTERM 会丢失，`hifimate-dlna` 因此设了 10 秒的停止超时。

### 5.4 AirPlay 的网卡

Shairport 默认在所有网卡（包括 docker0、Compose 网桥、lo）上广播。MA 用 host 网络能看到所有广播，可能选中网桥地址，
导致时钟同步失败而无声。因此每次启动前 `airplay_net.py render` 按默认路由（IPv4 优先、metric 最小）把 `interface` 写入运行时的配置副本
（已显式配置的保留，没有默认路由时保持自动）；`hifimate-airplay-net.service` 监听 `ip monitor route`，默认路由网卡变化时
（2 秒合并）重启 Shairport，并每 60 秒复核一次。

### 5.5 cork

当前 owner 自己的流只是被 cork（同一 serial、仍在获准的 lane）时保留已验证的链路：AirPlay 在交接引起的图时钟变化后会 flush 并重新同步，
期间短暂 cork；如果为此拆掉重建链路，会再次让图停顿而形成循环。cork 时没有 PCM 即静音，长时间无声由空闲释放处理，流真正消失仍按失败处理。

## 6. 输出设备

`output_devices.py` 从 `/proc/asound` 识别可用的输出，不打开设备：

- **USB**：读取所有 stream 文件里双声道 S32_LE 的 alternate setting，不混入 DSD 和单声道能力。proc 的 PCM info 通常没有 endpoint 信息，
  只有唯一的 playback stream 和唯一的 playback PCM 时才能一一对应；不能证明对应关系时拒绝，不按编号猜。
- **本机模拟输出**：把每个 playback PCM 的 info 与 codec 中同名、同设备号的非 Digital 双声道 Audio Output 对应，要求有明确的 Analog 证据，
  排除 HDMI、DisplayPort、S/PDIF。相关的 Speaker 控制转换器参与能力交集。缺少独立 PCM 能力时不推断继承。

运行时只处理 44.1–768 kHz，要求双声道 S32_LE，不重采样；重复的 card ID、连续或无效的采样率都拒绝。`auto` 优先唯一兼容的 USB，
没有 USB 才用唯一的本机输出，多个 USB 时不会退到本机。运行中明确选定的设备失败时不换别的硬件。

切换输出是一个事务：先写私有的 `.hifimate-output-transaction.json`（原始和目标内容及校验），再原子替换配置并只更新这一项的哈希和清单签名，
目录锁串行化。活动的仲裁服务只有在它的链接属于这个安装时才重启；失败时回滚配置和清单并重启旧配置。进程中断后用
`hifimate-output --recover --apply` 恢复；安装和卸载遇到未完成的事务会要求先恢复。

## 7. Music Assistant 整合

### 7.1 播放路径

MA 的官方 MPD Players provider 连接 `127.0.0.1:6601`，MPD 下载 MA 发布的 HTTP 音频流（8097）并输出到 `music_assistant_dlna_in`
（名称沿用早期设计，现在与 DLNA 无关）。MA 和 nginx 用 host 网络，所以 loopback 指的就是主机。没有专用的 renderer、SSDP 或网络命名空间。

MPD provider 强制 Flow 模式：整个队列是一路连续的流。Flow 的采样率选 bit perfect 时，跨采样率换曲会重启流，产生一次 stop/play，
HiFiMate 按新会话处理，迟到的旧 stop 受会话隔离保护。MPD 状态无法区分这种自动重启和用户点播放，所以其他输入接管后，
如果 MA 恰好又重启了流，可能再次抢占；没有加入推测性的延时屏蔽。

### 7.2 镜像和加载器

默认镜像 `ghcr.io/neqq3/ma-custom-loader-amd64:2.10.9` 是官方 `ghcr.io/music-assistant/server:2.10.9` 的薄包装：启动前把
`MUSIC_ASSISTANT_CUSTOM_PROVIDERS_DIR` 里的 provider 复制进 MA 的 provider 目录，然后以安装用户的 UID/GID 运行官方入口，不修改 MA 代码。
安装器每次把随附的 Bilibili、咪咕 provider（`music-assistant/providers/`）复制进这个目录。

`ma-provider-subscriber` 按 `provider-subscriber/options.json` 的 `sources` 从 GitHub 下载 provider 到同一个目录，默认为空。
它与 MA 使用相同的 UID/GID。这些目录是运行时数据，不在安装清单里；卸载只删除空目录。

`migu-api`（[migu-api-enhanced](https://github.com/Domdkw/miguMusic-api-enhanced)）上游没有发布镜像，Compose 从固定的 commit 本地构建，
所以安装器用 `pull --ignore-buildable`。它登录后会在请求 URL 里看到咪咕 token 并写进容器日志，因此只监听 loopback。
所有容器绑定宿主机的 `/etc/localtime`，`MUSIC_ASSISTANT_TZ` 由安装器从系统时区读取。

### 7.3 安装器对 MA 做的事

- **首个用户**：MA 没有用户时，用 MA 自己的首次设置接口（`POST /setup`）创建管理员（`scripts/ma_setup.py`）。必须先有管理员，
  因为 MA 把第一个用户当作所有者。
- **`hifimate-ctrl` 用户和令牌**：短暂停止 MA 服务器，在 MA 镜像里用 MA 自己的数据库和 JWT 代码（`hifimate_ctrl_token.py`）
  创建角色为「用户」（只能控制播放器和队列）的 `hifimate-ctrl`，签发十年期令牌写入 `config/ma-token`（0600，不打印）。
  这个用户没有密码，不能登录网页；在 MA 界面里给它设的密码会在重装时清除。令牌剩余超过一年时重装沿用，签发新令牌时旧的作废。
- **MPD 播放器**（`configure_ma_players.py`，只在 MA 停止时写 `settings.json`）：没有 MPD Players provider 时添加一个，
  手动地址为 `127.0.0.1:6601`；给 `mpd_127.0.0.1_6601` 补上缺少的设置：WAV、DAC 与 MA 共有的采样率（16 和 24 bit）、
  Flow 采样率 bit perfect、Native 音量。已有的值不改；添加过的 provider 被用户删除后不再添加（记录在数据目录的
  `.hifimate-ma-players.json`）。MA 启动后按这些设置创建播放器，与用户在界面里添加的相同（2.10.9 实测）。
- **其他播放器设置**（同一脚本）：MA 会把同一主机的 HiFi DLNA 合并进
  `<名称> AirPlay` 通用播放器，并让它的音量甚至回退播放走 DLNA 这一路，因此禁用其中 HiFiMate 自己的 DLNA 协议（MA 界面里可见、可恢复）；
  把 `<名称> AirPlay` 和 MPD 播放器队列的音量归一化设为 Disabled（仅当仍为 global 或未设置时）。首次安装会等待 MA 发现 `<名称> AirPlay`，最多 120 秒。
- **MilkDrop 可视化插件**：只在缺失时创建并启用，用户禁用后不会重新启用。
- 重装前把部署配置（`compose.yml`、`.env`、nginx 配置）和镜像清单备份到数据目录的 `.hifimate-deployment-backups/`。
  安装器不写 MA 的数据库（除上面的 `settings.json` 初始化），用户 `.env` 重装保留。

### 7.4 nginx

`web` 容器在 80 端口反代 MA（8095），把 `/ctrl/` 转到 5009、`/_hifimate/` 转到 5008，并保留 WebSocket；
443 端口用本地证书提供同样的内容，供房间校正页使用麦克风；`/roomfit` 重定向到 `/ctrl/roomfit/`，旧的 `/spectrum/` 重定向到 `/ctrl/#spectrum`。
nginx 在 MA 自身脚本之前注入频谱浮窗和 `spectrum-tap.js`。

## 8. 频谱

频谱显示 DAC 此刻在播放的声音：被动采集 DSP 的输入，乘以 CamillaDSP 离线测得的整条链响应和实时推子，按播放缓冲对齐。
不打补丁、无人观看时不采集。数据来源、分析方法、资源边界和显示细节见 [SPECTRUM.md](SPECTRUM.md)。

## 9. 控制页

### 9.1 服务

`ctrl.py` 基于标准库 ThreadingHTTPServer，监听 127.0.0.1:5009，提供 `/ctrl/` 页面、状态快照、日志和 SSE。所有订阅者共享一个约 1 秒的采集器，
最后一个订阅者离开后停止轮询。只读数据来自有超时的 systemctl、pactl、pw-dump、仲裁 IPC、CamillaDSP 只读 RPC 和输出识别；
未知的指标显示为未知，不把服务 active 等同于 DAC 在发声。频谱复用 5008 的那一份分析。

服务操作使用固定的 argv 白名单，不经过 shell；控制页自身和 PipeWire/WirePlumber 只允许重启。MA 的五个容器作为一组管理。
操作异步执行并报告接受/完成/失败，页面重连不会重放 POST。蓝牙配对作为一个临时的用户服务运行，SIGINT 让它在 finally 中关闭可发现状态。

### 9.2 安全

- HiFiMate 不保存控制密码：它就是运行用户（系统安装时是 `hifimate`）的 Linux 密码，`ctrl_auth.py` 用 ctypes 调 libpam 验证
  （服务 `hifimate`，`/etc/pam.d/hifimate` 只有 pam_unix）。控制服务以这个用户运行，pam_unix 通过 setgid 的 `unix_chkpwd` 允许
  非特权进程验证自己用户的密码，不能验证别的用户（已在 Debian 13、Fedora 43、Arch 实测），也不需要读 `/etc/shadow`。
  因此 `hifimate-ctrl.service` 不设 `NoNewPrivileges`（它会去掉 `unix_chkpwd` 的 setgid），也不设 `PrivateTmp`、`ProtectSystem`
  （用户服务里这些选项会让服务进入单独的用户命名空间，那里没有 shadow 组），否则验证一律失败；服务本身是没有 sudo 权限的普通用户。
  只调用 `pam_authenticate`：pam_unix 的 account 检查需要 root（非 root 时每次都记一条 setuid failed），对 nologin 的服务账号也没有意义，
  锁定的密码在认证这一步就失败。验证失败时 pam_unix 自带约 2 秒延迟，验证在锁外进行，不阻塞其他请求。`hifimate` 账号的 shell 是 nologin，并由
  `/etc/ssh/sshd_config.d/hifimate.conf` 的 `DenyUsers` 禁止 SSH，所以这个密码只用于控制页和 Samba 共享（`smbpasswd` 设为同一个）。
- 服务只在内存保存随机登录 token 的散列，30 天有效；`hifimate password` 修改密码后重启控制服务，所有会话失效。
- cookie 为 HttpOnly、SameSite=Strict，限定 `/ctrl/`；POST 校验 JSON 和同源的 Origin/Host；登录限频（每个地址 5 次，全局 20 次）。
- 账号还没有密码（锁定）时任何密码都验证不过；没有 libpam 时拒绝登录和所有写操作。
- 局域网的 HTTP 不加密，跨不可信网络应使用 HTTPS 反代或 SSH 隧道。日志对局域网可读，所以 HiFiMate 不在日志里输出任何凭据。
- 服务以安装用户运行，能访问该用户的 systemd、Docker 和 BlueZ；它不是对本机恶意用户的隔离。

### 9.3 正在播放

| 输入 | 曲目信息 | 控制 |
|---|---|---|
| DLNA | MPD 的标签 | MPD：播放/暂停、上下首、拖动 |
| Music Assistant | MA 的队列接口（用 `hifimate-ctrl` 的令牌） | MA 的队列：播放/暂停、上下首、拖动 |
| AirPlay | Shairport 的 metadata | DACP：播放/暂停、上下首，不能拖动 |
| 蓝牙 | BlueZ（AVRCP） | BlueZ：播放/暂停、上下首，不能拖动 |

MA 把整个队列作为一路 Flow 流送给 MPD，MPD 只知道「Music Assistant」，所以 MA 的曲目、进度、下一首和拖动都来自 MA 的队列接口。
没有令牌时只显示「Music Assistant」，播放/暂停仍可用。

MA 页面的浮窗选「HiFiMate 输出」时用控制页的接口（同一浏览器登录过控制页才能操作）；选「本浏览器」时用 MA 页面自己的登录信息
（`ma_access_token`）和网页播放器 id（`sendspin_webplayer_id`）向 MA 读取和控制。

### 9.4 歌词

`lyrics.py` 只查当前在播放的曲目，结果按曲目缓存：

1. 来自 MA 咪咕 provider 的曲目，直接用歌曲 id 取咪咕歌词。
2. 其他曲目在网易云匹配：先 `/search/match`（带 md5 的 persistId），再 `/cloudsearch`；要求歌名一致、歌手一致、时长相差 4 秒以内。
   有逐字歌词（`/lyric/new` 的 yrc）时逐字高亮。
3. 网易云没有时在咪咕搜索。
4. B 站视频的「歌手」通常是 UP 主，标题带修饰（如「周杰伦 - 晴天MV 2160P修复版」）：先清理标题，从「歌手 - 歌名」里拆出两者（两种顺序都试），
   不要求歌手一致，但歌名必须一致、时长相差 2.5 秒以内，且不接受「(原唱 …)」「(钢琴版)」这类其他版本。时长对不上时宁可不显示。

## 9.5 EQ 页面和设置页面

- **EQ**（`hifimate/eq.py`、`gui/eq.*`）：文件 `config/dsp-filters.yml` 仍是唯一的数据来源，页面编辑草稿。读取时返回文件内容的指纹，
  保存时指纹不符（在主机上被改过）就拒绝；保存前把旧内容存为版本（`state/eq/versions/`，最近 10 个），原子替换后让仲裁服务
  重新加载（不重开声卡）。曲线：草稿、房间校正和两者合计分别交给离线 CamillaDSP（`dsp_model.py`，48 kHz、32768 点）测量，
  取 H[c][c] 的幅度，按对数频率插值成 240 个点；同一份配置有缓存，一次测量约 0.1 秒。合计的最大提升加上预设的 Headroom 超过 0 dB
  时提示可能削波。导入解析 REW、AutoEQ、Equalizer APO 的 `Filter N: ON PK Fc … Gain … Q …` 格式（含 `Preamp`、`Channel`、`BW Oct`）。
  页面把每个 (pipeline 步骤, 滤波器) 当一行，保存时每行一个步骤，滤波器的开关就是步骤的 `bypassed`。
- **旁路对比**：仲裁服务内存里的开关，打开时给所有个人滤波器的步骤加上 `bypassed`，文件不变；服务重启或关闭开关就恢复。
- **设置**（`hifimate/settings.py`、`gui/settings.*`）：播放设置写在 `config/settings.json`（用户数据，不在安装清单里），
  仲裁服务启动时叠加在 `hifimate.conf` 上，切换输出时的采样率检查也读它。音乐源订阅直接改订阅器的 `options.json`（其他键保留），
  然后作为一个异步操作：重启订阅器，等日志里出现这一轮的「Next check in」，把错误和警告记入事件，再重启 MA 服务器。

## 10. 房间校正

### 10.1 测量

测量页（`/ctrl/roomfit/`，HTTPS）在 iPhone 上关闭回声消除、降噪和自动增益，无损上传录音（每次约 3 MB）。扫频由 DLNA 的 MPD
经过真实链路（仲裁 → CamillaDSP → DAC）播放，会中断当前的音乐。主机反卷积得到冲激响应，加窗后得到频响，再扣除所选手机的麦克风曲线
（补偿最多 12 dB；麦克风需要补偿超过 6 dB 的低频段标为不可信，例如 iPhone 15 Pro Max 约 30 Hz 以下）。

向导流程：

1. 在中心位置扫一次，按实测信噪比选择扫频电平（目标 38 dB，录音峰值不超过 -6 dBFS），音量不足时提示调高。
2. 中心及左、右、前、后约 30 cm，共 5 个点，至少 3 个。每点扫左右声道，判定好用/可以用/需要重录，并说明原因（削波、信噪比、
   iOS 音频处理、与中心差异过大）。
3. 各点 1/6 倍频程频响取 dB 平均（中心权重 2），先扣除已开启的房间校正；在 35–350 Hz 拟合只削峰的 PEQ
   （目标 60 Hz 以下 +3 dB，1/3 倍频程拟合，Q ≤ 5，每声道最多 8 个），显示校正前后的预测。
4. 应用时写入 `config/room-correction.json`，正在播放时立即生效、不重开声卡。最近 3 组方案保存在 `state/roomfit/profiles/`。

### 10.2 麦克风曲线

`hifimate/mics/*.json` 每个文件是一台实测样机的曲线，写明原文链接和测量方法：Faber Acoustical 在消声室里把实验室参考麦克风
（15 Pro Max 到 17 Pro 用 PCB 378B02 1/2″，18 Pro 用 PCB 377C01 1/4″）贴近 iPhone 底部麦克风，用 SignalScope 的双通道 FFT 和 iOS 测量模式测得，
按参考麦克风的出厂数据做压力响应校正（图中蓝色曲线）。HiFiMate 从发表的图中按坐标网格读取数值，每 1/24 倍频程取中值，误差约 ±0.3 dB；
贴在图底边被截断的点不收录。每个文件的 `models` 列出沿用这条曲线的同代机型。

### 10.3 证书

iPhone 只允许 HTTPS 页面使用麦克风。安装时 `tls.py` 生成本地 CA（`config/tls/`，重装保留）和服务器证书（局域网 IP 和 `<主机名>.local`，
有效期 397 天）；地址变化或临近过期时安装器重签，CA 不变。

## 11. 蓝牙

- WirePlumber 只注册 A2DP Sink（接收媒体音频，不提供 HFP/HSP 通话），并关闭座席监视：无头运行时用户会话不在活动座席上，
  开着座席监视 BlueZ 监视器永远不会启动，也就不会公布 Audio Sink。
- 蓝牙输入节点路由到 `bluetooth_in`，音量只由手机控制（AVRCP 绝对音量），WirePlumber 不恢复也不保存它。
- `hifimate-bluetooth.service` 通过 BlueZ `Adapter1.Alias` 给所有适配器设置名称（包括之后插入的），按 MAC 地址记录原名称到
  运行用户的 `~/.local/state/hifimate/`；卸载只还原仍属于 HiFiMate 的名称，管理员后来改过的保留。它还每 10 秒确认配对窗口之外不可发现
  （bluetoothd 重启后默认是可配对的）。
- 配对窗口期间注册一个代理，自动确认配对；第一台成功配对的设备被设为受信任，窗口随即关闭。
- `--setup-bluetooth-speaker` 用 sudo 在 `/etc/bluetooth/main.conf` 写入带标记的设备类别（Class 0x000414，音频/视频 · 扬声器）和
  `JustWorksRepairing = confirm`（允许已配对过的手机经配对窗口的代理重新配对；BlueZ 默认的 never 会拒绝，手机显示 PIN 错误），然后重启 bluetoothd。
  已有管理员设置的 Class 时拒绝。归属记录在 `/var/lib/hifimate-bluetooth-speaker/<UID>.json`，卸载只删除未改动的受管行。
- BlueZ 用设备与 MediaPlayer 路径绑定会话，同一设备旧播放器的暂停或移除不影响新播放器。

## 12. 音量

四路输入的音量都只由各自的控制端调整（DLNA 控制端、MA、AirPlay 发送端、手机）。WirePlumber 不恢复也不保存这些流的音量
（`state.restore-props = false`），新流总是从 0 dB 开始；否则两路 MPD 共用 `media.role=Music` 的记录，一路的音量会串到另一路
（例如 54% 即 -16 dB）。MA 的 Volume Control 选 Native 时，MA 调的是 MPD 6601 的 PipeWire 流音量。CamillaDSP 的主音量独立于这些。

## 13. 实时调度

- `realtime.py` 是 exec 前的小包装，只用于 CamillaDSP、两路 MPD 和 Shairport。`auto`（默认）先读 `/sys/kernel/realtime`，
  再读 `/proc/config.gz` 或 `/boot/config-<release>` 的 `CONFIG_PREEMPT_RT`，确认是实时内核才申请；`on` 在普通内核也申请；`off` 不申请。
- 申请时先把 RLIMIT_RTTIME 限制为最多 5 秒（实测 MPD 主线程启动约 220 ms、Shairport 启动线程约 1.5 秒不阻塞，200 ms 会让它们被 SIGKILL），
  再申请 SCHED_RR 优先级 5。权限不足时写日志并以普通调度继续。不改内核的 `sched_rt_runtime_us` 节流。
- PipeWire 自己的 module-rt 默认请求 83/88 级，超过 RLIMIT_RTPRIO 5 时退回 RTKit，而无头用户不在活动会话里，polkit 会拒绝 RTKit，
  音频线程就静默保持普通调度。因此受管的 `52-hifimate-realtime.conf` 片段把 `rt.prio` 设为 5、不调 nice（RLIMIT_NICE 为 0 时 nice 失败也会关闭实时）、
  关闭 RTKit 和 portal；WirePlumber 忽略 `module.rt.args`，所以它的片段追加一个同参数的 module-rt。验证：`ps -L -o cls,rtprio,comm -p <pid>`
  中 `data-loop.0` 为 `FF 5`。
- `--setup-realtime` 用 sudo 只创建 `/etc/systemd/system/user@<UID>.service.d/90-hifimate-rtprio.conf`，把 RTPRIO 的软硬上限提到至少 5，
  保留更高的值；不改 PAM，不重启 user manager（所以要重新登录后生效）。归属和文件 SHA256 记录在 `/var/lib/hifimate-realtime/<UID>.json`，
  绑定 UID 和安装目录；用户配置显式把 `DefaultLimitRTPRIO` 设得更低时，在调用 sudo 之前报告冲突。

## 14. 安装器

- **系统安装和用户安装**：`sudo hifimate install`（一键安装脚本也是它）以 root 运行 `hifimate/system.py` 的步骤：
  安装软件包，创建用户 `hifimate`（普通 UID 而不是 `useradd --system`，nologin，主目录 `/var/lib/hifimate`，加入 audio、bluetooth、docker 组），
  准备 `/opt/hifimate` 和 `/srv/hifimate/music`（setgid 2775），用 `realtime_permissions.py`、`bluetooth_speaker.py` 以该用户的 UID
  做两项 root 设置，`loginctl enable-linger` 并启动 `user@<UID>.service`（新加了组或新的 RTPRIO 上限时重启它，否则服务拿不到），
  然后用 `runuser -u hifimate` 加上该用户的 `XDG_RUNTIME_DIR` 和会话总线运行 `scripts/install.sh`，最后写 Samba 共享
  （`smb.conf` 末尾带标记的一段、`smbpasswd`、Avahi 的 `_smb._tcp` 服务文件、firewalld 放行）。
  密码由 root 读取（文件可以只有 root 能读），经环境变量交给 `install.sh`。安装选项记在 `/etc/hifimate/install.json`。
  非 root 运行 `hifimate install` 则是用户安装：装在当前用户的 `~/hifimate`，不建用户、不共享，系统设置要显式加选项并用 sudo。
  系统安装存在时，普通用户运行 `hifimate` 会被提示改用 sudo。
- **为什么是普通 UID**：journald 只给系统 UID 范围之外的用户单独的日志文件。`hifimate` 有了自己的日志文件，控制页读自己服务的日志
  不需要任何额外权限；如果是系统用户，就得加入 systemd-journal 组，那样能读整台机器的日志，而控制页的日志对局域网可见。
  读日志时仍显式限定 `_UID=` 为自己。
- **两种入口，一条安装路径**：`hifimate` 命令（pipx 包）把 wheel 里的 `hifimate/_dist/` 和 `hifimate` 包在临时目录里还原成源码树，
  再运行与源码 checkout 相同的 `scripts/install.sh`、`scripts/uninstall.sh`。安装器只用标准库；运行时依赖来自发行版的软件包
  （`scripts/host_stack.py` 按 `/etc/os-release` 选择 apt、dnf、pacman 或 zypper，软件包清单在 `config/host-stack-manifest.json`）。
- **CamillaDSP**：`scripts/camilladsp_release.py` 下载官方 4.1.3 的 PulseAudio 版本，核对固定的 SHA256、`--version` 和 `--help` 里的
  `pulse-backend`，装到 `bin/hifimate-camilladsp`，来源记录在 `share/hifimate/camilladsp/release.json`，档案缓存在 `~/.cache/hifimate`。
  离线安装用 `--camilladsp-archive`（非固定档案时加 `--camilladsp-sha256`）。
- **归属清单**：安装目录里的 `.hifimate-install-manifest` 记录每个自有文件的哈希，`.hifimate-user-config-manifest` 记录发布到
  运行用户 `~/.config` 的 PipeWire/WirePlumber 片段和 unit 链接，两者都带签名文件。重装和卸载前先核对全部哈希，检查目标及父目录没有符号链接或未归属的文件；
  被手工修改的自有文件拒绝覆盖或删除。用户数据（MA 令牌、个人滤波器、房间校正、证书、测量记录、MA 的 provider 目录）
  在清单之外，重装保留。安装中途失败时保留 `.hifimate-install-in-progress` 和错误信息以便重试。
- **原子替换**：升级自有片段时先暂存全部新文件、清单和签名，再逐文件原子替换，失败回滚。
- **systemd 单元**：安装时把 `@PREFIX@` 等占位符渲染为绝对路径，宿主程序（mpd、upmpdcli、shairport-sync）按 PATH 解析为实际路径；
  找不到时保留命令名，由 systemd 自己的搜索路径查找。`config/hifimate.conf` 的 `base_config` 每次安装都指向当前安装目录，安装目录被移动后
  房间校正和个人滤波器仍能找到。
- **root 操作**：只有 `--install-deps`、`--setup-realtime`、`--setup-bluetooth-speaker` 用 sudo；后两者的归属记录是 root 所有的文件，
  卸载按记录还原，不依赖用户可写的标记。
- **无人值守**：密码只从环境变量或文件读取（`scripts/install_secrets.py`），在任何改动之前校验。

## 15. 验证状态与已知限制

已经实机验证（Debian 13、一台支持到 768 kHz 的 USB DAC、MA 2.10.9）：

- 192 kHz/24 bit 文件经 MA 播放，PipeWire、DSP、DAC 均为 192 kHz，ALSA 回读 S32_LE，`resampled=false`；Flow 队列 192 ↔ 44.1 kHz 换曲跟随到 DAC。
- DLNA 与 MA、AirPlay 与 MA 之间双向接管，原来的一路暂停；迟到的旧 stop 不撤销新 owner，停止后不恢复前一路。
- 在临时的 PipeWire/WirePlumber 图里协商 44.1 kHz → 768 kHz → 44.1 kHz/24 bit（`scripts/check-format-stack.py`，不打开声卡），
  MPD 0.24 原生 PipeWire 输出同样通过。
- 频谱与打补丁读取 ALSA 输出的方法逐帧一致（见 [SPECTRUM.md](SPECTRUM.md)）。

自动测试覆盖四路输入之间全部 12 个切换方向、准备失败、旧会话、远程暂停不支持、恢复、同源格式变化和淡入取消；
外部命令、DSP RPC 和接收端都是假的，不能替代实际播放。

尚未完成或有限制的：

- 蓝牙与其他输入的实机切换、各种发送端的远程暂停兼容性、淡入听感、空闲时的整机 CPU 测量。
- 除 Debian 13 外的发行版只验证了依赖安装和检查，没有实机播放。
- MPD idle 和 BlueZ 的极短状态变化可能在回读前合并；观察不到的 play/resume 不会凭持续的 PCM 猜测，用户可以暂停再播放。
- 不带 connection-number payload 的 AirPlay metadata 对连接的区分有限。
- 共享 DAC 的切换有一段静音间隙，不是无缝切换。
- 每个采样率只有在实际设备上播放过才算支持；DAC 公布的能力、配置校验和隔离测试都不等于实际播放通过。
