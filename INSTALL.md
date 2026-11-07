# 安装与维护

本文按顺序说明：先确认前置条件，再安装，然后完成首次设置。日常维护、卸载和故障排查在后面。
安装后各个文件放在哪里，见 [README 的「安装后的文件」](README.md#安装后的文件)。
从源码开发、测试和发布的说明集中在最后一章「[开发者](#8-开发者)」。

## 1. 前置条件

开始安装前，逐项确认下面几点。

### 1.1 操作系统

需要 PipeWire 1.0 及以上、WirePlumber 0.5 及以上。下表中的系统都已在官方容器里实际安装依赖并通过检查；
完整的播放链路在 Debian 13 上实机验证过。

| 系统 | 支持情况 |
|---|---|
| Debian 13 (trixie) 及更新 | 支持（主要开发平台）。upmpdcli 需先添加[上游软件源](https://www.lesbonscomptes.com/upmpdcli/pages/downloads.html) |
| Ubuntu 26.04 LTS 及更新 | 支持。upmpdcli 需先添加 PPA `ppa:jean-francois-dockes/upnpp1` |
| Ubuntu 24.04 LTS | 支持，但要先升级 PipeWire（见下） |
| Fedora 43 及更新 | 支持。upmpdcli 用[上游提供的 RPM](https://www.lesbonscomptes.com/upmpdcli/pages/downloads.html) |
| Arch Linux | 支持，所有软件包都在官方仓库 |
| openSUSE Tumbleweed | 支持，所有软件包都在官方仓库 |
| Debian 12 | 不支持：PipeWire 0.3、WirePlumber 0.4 太旧，请升级到 Debian 13 |
| RHEL、CentOS Stream、Rocky、AlmaLinux | 暂不支持（见下） |

**Ubuntu 24.04 LTS** 自带 WirePlumber 0.4，读不了 HiFiMate 的配置。先用 PipeWire 上游维护者的 PPA 升级到
PipeWire 1.0.7、WirePlumber 0.5.2，再安装 HiFiMate：

```sh
sudo add-apt-repository ppa:pipewire-debian/pipewire-upstream
sudo add-apt-repository ppa:pipewire-debian/wireplumber-upstream
sudo add-apt-repository ppa:jean-francois-dockes/upnpp1      # upmpdcli
sudo apt update && sudo apt full-upgrade
systemctl --user restart pipewire pipewire-pulse wireplumber  # 或者重新登录
```

这两个 PPA 替换的是整台机器的音频核心组件，会影响机器上所有发声的程序。专门用来放音乐的主机，建议直接装 Ubuntu 26.04 LTS 或 Debian 13。

**RHEL、CentOS Stream、Rocky、AlmaLinux 暂不支持**，因为三个必需的组件在这些系统的官方源和 EPEL 里都没有：
MPD（DLNA 和 Music Assistant 两路输入都靠它）、upmpdcli（DLNA 接收）和 python3-dbus-next（蓝牙）。
要支持，只能由我们自己编译并长期维护这些软件包，或者把它们放进容器（声卡、PipeWire、蓝牙都要穿透进容器），
两条路的维护和验证成本都很高。想用红帽系的系统，可以改用 Fedora。

### 1.2 硬件

- CPU 为 x86_64 或 aarch64（CamillaDSP 官方只提供这两种的 PulseAudio 版本）。
- 一台 USB DAC，要求支持双声道、32 位容器（S32_LE）的固定采样率列表。常见的 USB 音频 DAC 一般都满足，安装时会检查。
  没有 DAC 时可以用主机自带的模拟输出；HDMI、DisplayPort、S/PDIF 不支持。
- 使用蓝牙时需要蓝牙适配器（主机内置或 USB）。
- 声卡由 HiFiMate 独占。主机上其他用户（例如登录桌面的人）自己的 PipeWire 也会使用这块声卡，见 [1.4](#14-用户和权限)。

### 1.3 软件

- **系统软件包**：PipeWire、WirePlumber、BlueZ、MPD、upmpdcli、Shairport Sync、Avahi，以及 Python 3.10+ 和
  NumPy、PyYAML、websocket-client、dbus-next。安装器会用系统的包管理器（apt、dnf、pacman、zypper）自动安装，
  只有上表提到的 upmpdcli 软件源需要你事先添加；没添加时安装器会提示从哪里获取。
- **CamillaDSP 4.1.3**：安装器从 GitHub 下载官方版本并按固定的 SHA256 校验，不需要自己编译。
- **Samba**：共享音乐库用，安装器自动安装（不共享时加 `--without-share`）。
- **pipx**：一条命令安装时，如果系统里没有，安装脚本会自动装上。
- **Docker Engine 和 Compose v2**：使用 Music Assistant 时需要，安装器不会自动安装，请按
  [Docker 官方说明](https://docs.docker.com/engine/install/)先装好并启动。

### 1.4 用户和权限

- 安装需要 root：用 `sudo` 运行安装命令。安装器会创建一个专门运行 HiFiMate 的用户 `hifimate`
  （不能登录，主目录 `/var/lib/hifimate`），把它加入 `audio`、`bluetooth`、`docker` 组，并让它开机自动运行，不需要有人登录。
  之后日常运行不再需要 sudo；管理命令 `sudo hifimate …` 本身用 sudo。
- `hifimate` 用户的 Linux 密码就是控制页密码，也是音乐共享的密码。这个账号的 shell 是 nologin，安装器还禁止它通过 SSH 登录，
  所以这个密码只能用来登录控制页和访问共享。
- 实时调度上限和蓝牙音箱类别这两项系统设置，安装器直接替 `hifimate` 用户设好（见 [3.4](#34-安装器做的系统设置)），卸载时还原。
- **这台主机最好专门用来放音乐。** 其他用户登录桌面时，他们自己的 PipeWire 也会使用同一块声卡，
  可能和 HiFiMate 抢声卡。在只用 SSH 管理的主机上，可以停掉管理员账号自己的音频服务，
  让声卡只归 HiFiMate（用管理员账号运行，以后要恢复把 `mask` 换成 `unmask`）：

  ```sh
  systemctl --user mask --now pipewire.socket pipewire-pulse.socket pipewire.service pipewire-pulse.service wireplumber.service
  ```

### 1.5 网络和端口

主机和手机、电脑需要在同一个局域网，并且局域网允许组播：AirPlay 用 mDNS、DLNA 用 SSDP 发现设备。

| 端口 | 用途 | 对局域网开放 |
|---|---|---|
| 80 | Music Assistant 和控制页（`/ctrl/`） | 是 |
| 443 | 房间校正页面（HTTPS） | 是 |
| 8095、8097 | Music Assistant 的网页接口和音频流 | 是（Music Assistant 自身需要） |
| AirPlay、DLNA 的端口 | 由 Shairport Sync、upmpdcli 自动选择 | 是 |
| 6600、6601、5008、5009、3003、3004、1234 | 内部服务 | 否，只监听 127.0.0.1 |

80 端口被占用时可以换一个，见 [Music Assistant 使用说明](music-assistant/README.md#更换网页端口)。

## 2. 安装

### 2.1 一条命令安装

```sh
curl -fsSL https://github.com/hifimate/hifimate/releases/latest/download/install.sh | sudo sh
```

这条命令依次完成：

1. 没有 pipx 时，用系统的包管理器安装 pipx。
2. 用 pipx 安装 `hifimate` 命令（`/usr/local/bin/hifimate`，软件包在 `/opt/pipx`）。
3. 运行 `hifimate install`：
   1. 安装系统软件包（包括 Samba），下载 CamillaDSP；
   2. 创建用户 `hifimate`，准备 `/opt/hifimate`（程序和设置）和 `/srv/hifimate/music`（音乐库）；
   3. 设置实时调度上限和蓝牙音箱类别；
   4. 以 `hifimate` 用户启动所有服务，部署 Music Assistant；
   5. 把音乐库共享为 `Music`。

安装过程中会询问：

1. **控制页密码**：至少 8 位。它就是 `hifimate` 用户的 Linux 密码，控制页登录时由系统（PAM）验证，HiFiMate 自己不保存密码；
   音乐共享（用户 `hifimate`）用同一个密码。
2. **设备名称**，例如 `客厅`。手机上看到的是 `客厅 AirPlay`、`客厅 DLNA`、`客厅 Bluetooth`。
   名称可以用中文、空格、字母、数字和 `-_.()`，最长 42 字节（约 14 个汉字）。
3. **输出设备**：只有一台兼容的 DAC 时自动选择；有多台时让你选。
4. **Music Assistant 管理员**：用户名和密码，用于登录 Music Assistant 网页。这一步不能跳过，
   HiFiMate 控制页对 Music Assistant 的控制依赖它。

要给安装加选项，把 `| sudo sh` 换成 `| sudo sh -s -- 选项…`，例如：

```sh
curl -fsSL https://github.com/hifimate/hifimate/releases/latest/download/install.sh | sudo sh -s -- --name 客厅 --without-music-assistant
```

已经装有 pipx 时，也可以分两步：`sudo PIPX_HOME=/opt/pipx PIPX_BIN_DIR=/usr/local/bin pipx install hifimate`，
然后 `sudo hifimate install 选项…`。

### 2.2 安装选项

| 选项 | 作用 |
|---|---|
| `--name 名称` | 设备名称；重新安装时省略则保留原名称 |
| `--output-device hw:CARD=卡ID,DEV=设备号` | 指定输出设备（查看可用设备见 [4.4](#44-更换输出设备)） |
| `--choose-output` | 列出可用的输出设备让你选 |
| `--music-directory 目录` | 音乐库的位置，默认 `/srv/hifimate/music`；已有的目录不改所有者，要让 `hifimate` 用户能读写 |
| `--without-share` | 不把音乐库共享到局域网 |
| `--without-music-assistant` | 不部署 Music Assistant（也就不需要 Docker） |
| `--without-ma-provider bilibili` 或 `migu` | 不安装某个自带的音乐源；`--without-ma-providers` 两个都不装 |
| `--realtime off` | 不使用实时调度 |
| `--no-install-deps` | 不调用包管理器，系统软件包由你自己装 |
| `--dry-run` | 只显示会做什么，不做任何改动 |
| `--non-interactive` | 无人值守，不询问任何问题（见 2.3） |

选项会记在 `/etc/hifimate/install.json`（不含密码）。以后再运行 `sudo hifimate install` 只需给出要改的选项，其他选项沿用上次的；
`sudo hifimate upgrade` 也沿用这些选项。要完全按新给的选项安装，加 `--reset-options`。
之前没装 Music Assistant、现在想加上，用 `--with-music-assistant`；之前没有共享、现在想加上，用 `--with-share`。

### 2.3 无人值守安装

`--non-interactive` 不询问任何问题。原本要询问的内容必须事先给出，缺少时在改动任何东西之前报错退出。
从管道运行且没有终端时，自动按无人值守处理。

密码不能放在命令行参数里（参数对本机所有用户可见），用只有 root 可读的文件或环境变量给出：

| 内容 | 用文件 | 或者用环境变量 |
|---|---|---|
| 控制页（和音乐共享）的密码 | `--ctrl-password-file 文件` | `HIFIMATE_CTRL_PASSWORD` |
| Music Assistant 管理员用户名 | `--ma-admin-user 名称` | `HIFIMATE_MA_ADMIN_USER` |
| Music Assistant 管理员密码 | `--ma-admin-password-file 文件` | `HIFIMATE_MA_ADMIN_PASSWORD` |

用文件（推荐）：

```sh
sudo install -m 600 /dev/null /root/hifimate.pw && sudo nano /root/hifimate.pw     # 写入密码
curl -fsSL https://github.com/hifimate/hifimate/releases/latest/download/install.sh | sudo sh -s -- \
  --name 客厅 --output-device hw:CARD=DAC,DEV=0 \
  --ctrl-password-file /root/hifimate.pw --ma-admin-user admin --ma-admin-password-file /root/hifimate.pw
```

用环境变量时，`sudo` 默认会丢掉它们，要用 `--preserve-env` 显式传过去。不要写成 `sudo VAR=… sh`，那样密码会出现在进程列表里：

```sh
export HIFIMATE_CTRL_PASSWORD='…' HIFIMATE_MA_ADMIN_USER=admin HIFIMATE_MA_ADMIN_PASSWORD='…'
curl -fsSL https://github.com/hifimate/hifimate/releases/latest/download/install.sh | \
  sudo --preserve-env=HIFIMATE_CTRL_PASSWORD,HIFIMATE_MA_ADMIN_USER,HIFIMATE_MA_ADMIN_PASSWORD sh -s -- --name 客厅
```

说明：

- 密码 8–1024 个字符，不能有换行；文件末尾的一个换行会被去掉。
- 重新安装时给出控制页密码会替换旧密码（`hifimate` 用户的 Linux 密码和共享密码一起换）；不给则保留。
- Music Assistant 已经有用户时，给出的管理员会被忽略，不会修改它的密码。

## 3. 安装后的设置

### 3.1 打开控制页

浏览器打开 `http://<主机IP>/ctrl/`。主机 IP 可以在主机上用 `hostname -I` 查看；用手机或其他电脑访问时不能填 `127.0.0.1`。
查看状态不需要登录，操作时用控制页密码登录，登录 30 天内有效。

### 3.2 设置 Music Assistant

HiFiMate 已经被设为 Music Assistant 的播放器（`<名称> MA`），第一次使用只需添加音乐源，步骤见
[Music Assistant 使用说明](music-assistant/README.md#首次设置)。

### 3.3 蓝牙配对

在控制页的蓝牙卡片里点「配对 120 s」，或在主机上运行 `sudo hifimate pair`（`--seconds` 可改为 10–600 秒），
然后在手机的蓝牙设置里选 `<名称> Bluetooth`。第一台配对成功的设备会被信任，窗口随即关闭。以后手机直接连接即可，不需要再开窗口。
手机以前配对过、在手机上删除后重新配对，也是同样的步骤。

确认系统的蓝牙服务和 Docker 服务是开机启动的：`systemctl is-enabled bluetooth docker`。

### 3.4 安装器做的系统设置

下面两项由安装器以 root 身份设好，卸载时自动还原：

- **实时调度上限**：允许 `hifimate` 用户的 CamillaDSP、MPD、Shairport Sync 和 PipeWire 音频线程以低优先级实时调度运行，
  在负载高或使用实时内核（PREEMPT_RT）时减少爆音。写在 `/etc/systemd/system/user@<UID>.service.d/90-hifimate-rtprio.conf`。
  不需要时安装加 `--realtime off`。
- **蓝牙音箱类别**：在 `/etc/bluetooth/main.conf` 里加两行带 HiFiMate 标记的设置，让手机把主机显示为音箱图标，
  并允许已配对过的手机重新配对。会重启一次蓝牙服务。`main.conf` 里已有你自己设置的 Class 时不会修改。

控制页密码：安装器写入 `/etc/pam.d/hifimate`（只用 pam_unix 验证密码），用 `chpasswd` 设置 `hifimate` 用户的 Linux 密码，
并写入 `/etc/ssh/sshd_config.d/hifimate.conf` 禁止这个用户 SSH 登录。这两个文件在 `uninstall --purge` 删除用户时一起删除。

另外，共享音乐库会在 `/etc/samba/smb.conf` 末尾加一段带标记的 `[Music]` 设置，并给 `hifimate` 用户设 Samba 密码；
`smb.conf` 里其他内容不动，已经有别的 `[Music]` 共享时不会修改。

### 3.5 让主机一直在线（可选）

主机用 Wi‑Fi 或者是笔记本时，下面的设置能避免断网和休眠。它们属于整台主机的策略，HiFiMate 不会自动设置，卸载时也不会还原。

**关闭 Wi‑Fi 省电**（只适用于用 NetworkManager 管理 Wi‑Fi 的主机）：

```sh
sudo tee /etc/NetworkManager/conf.d/20-hifimate-wifi-powersave.conf >/dev/null <<'EOF'
[connection]
wifi.powersave = 2
EOF
sudo nmcli general reload conf
```

重新连接 Wi‑Fi 后生效，用 `iw dev <网卡> get power_save` 检查。还原：删除这个文件，再执行 `sudo nmcli general reload conf`。

**合盖不休眠**（笔记本）：

```sh
sudo mkdir -p /etc/systemd/logind.conf.d
sudo tee /etc/systemd/logind.conf.d/60-hifimate-lid.conf >/dev/null <<'EOF'
[Login]
HandleLidSwitch=ignore
HandleLidSwitchDocked=ignore
HandleLidSwitchExternalPower=ignore
IdleAction=ignore
EOF
sudo systemctl restart systemd-logind
```

重启 `systemd-logind` 可能会让当前桌面会话退出，请在不用桌面时执行。还原：删除这个文件，再重启 `systemd-logind`。

**完全禁止休眠**（确认这台主机不需要任何休眠时）：

```sh
sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target suspend-then-hibernate.target
```

还原时把 `mask` 换成 `unmask`。

## 4. 日常维护

### 4.1 查看状态

```sh
sudo hifimate status
```

显示安装的版本、输出设备、音乐库，以及每个服务和 Music Assistant 容器是否正常。控制页的「服务」部分显示同样的信息和日志。
查看日志：

```sh
sudo hifimate logs              # 所有服务最近 100 行
sudo hifimate logs airplay -f   # 只看 AirPlay，持续输出；也可以是 dlna、bluetooth、camilladsp、mpd、ctrl 等
```

各服务的作用见 [ARCHITECTURE.md](docs/ARCHITECTURE.md#2-组成部分)。

### 4.2 升级

```sh
sudo hifimate upgrade                 # 升级到最新版本
sudo hifimate upgrade --version 0.2.0 # 升级或回到指定版本
```

升级会沿用上次安装的选项，并重启所有服务，正在播放的音乐会中断。你的密码、EQ、房间校正、音乐库和 Music Assistant 数据都会保留。

### 4.3 自己的 EQ

EQ 在 EQ 页面（`http://<主机IP>/ctrl/eq/`）编辑，见 [README](README.md#自己的-eq)。它保存在 `/opt/hifimate/config/dsp-filters.yml`，
也可以用 `sudo nano` 直接编辑；页面和手动编辑互相兼容，页面保存时如果发现文件在这期间被改过，会提醒而不会覆盖。规则：

- 只能有 `Filter` 步骤；滤波器类型可用 Biquad、BiquadCombo、Gain、Delay、DiffEq、Conv。Volume 和 Loudness 不允许。
- 声道写 `[0]`（左）、`[1]`（右）或 `[0, 1]`；定义的每个滤波器都要在 `pipeline` 里用到。
- 处理顺序是：预留余量（Headroom）→ 你的 EQ → 房间校正。所有采样率都用同一份 EQ。
- 手动改完运行 `sudo hifimate reload-filters`（页面上点「应用」即可）。写法有错时会说明原因，播放照常进行，只是不带你的 EQ。

### 4.4 更换输出设备

在控制页的输出卡片里切换，或者在主机上：

```sh
sudo hifimate output                               # 列出可用的输出设备
sudo hifimate output --choose                      # 选择并切换
sudo hifimate output --device hw:CARD=DAC,DEV=0    # 直接切换到指定设备
```

切换会中断正在播放的音乐。列表里的名称以「USB」或「本机」开头，后面是 `hw:CARD=…,DEV=…` 形式的设备 ID，
也可以把 ID 用于安装选项 `--output-device`。

输出设备是 `auto` 时：只有一台兼容 USB DAC 就用它；没有 USB DAC 时用唯一的本机模拟输出；
有多台时需要明确指定。

### 4.5 修改名称和密码

- 改名称：`sudo hifimate install --name 新名称`。
- 改控制页密码：`sudo hifimate password`。它修改 `hifimate` 用户的 Linux 密码和音乐共享的密码，并让所有已登录的浏览器重新登录。
- Music Assistant 的密码在 Music Assistant 网页里修改。

### 4.6 其他配置

在设置页面（控制页右上角的齿轮）修改：

| 设置 | 默认 | 作用 |
|---|---|---|
| 淡入 | 400 毫秒 | 开始播放时从静音升到正常音量，20–2000 毫秒 |
| 释放 DAC | 5 分钟 | 没有声音多久后暂停发送端并关闭 DAC，可以设为不释放 |
| 允许的采样率 | DAC 支持的全部 | 可以排除个别采样率；被排除的曲目会报错并保持静音，不会重采样 |

保存后仲裁服务会重启，正在播放的音乐中断几秒。这些设置保存在 `/opt/hifimate/config/settings.json`，重新安装和升级都会保留。

`/opt/hifimate/config/hifimate.conf` 是安装器管理的默认值，记录在安装清单里，**不要手改**：改过之后，下次安装或升级会拒绝继续。
要在主机上改上面这几项，编辑 `settings.json`（键名 `fade_ms`、`idle_timeout_s`、`rates`），再运行 `sudo hifimate restart`。

## 5. 卸载

```sh
sudo hifimate uninstall --dry-run   # 先看看会删除什么
sudo hifimate uninstall
sudo PIPX_HOME=/opt/pipx PIPX_BIN_DIR=/usr/local/bin pipx uninstall hifimate   # 最后删除 hifimate 命令本身
```

卸载会停止并删除 HiFiMate 的服务和程序，删除 Music Assistant 的容器，还原蓝牙名称，撤销实时调度上限、蓝牙音箱类别和音乐共享。
下面这些会保留，再次安装时会继续使用：

- 系统软件包（PipeWire、BlueZ、MPD、Samba 等），它们可能还有别的用途；
- 音乐库 `/srv/hifimate/music`；
- 用户 `hifimate`（和它的密码），以及属于你的数据：`/opt/hifimate` 里的 EQ、房间校正和测量记录、本地证书、
  Music Assistant 的设置和另行安装的音乐源，`/var/lib/hifimate` 里的 Music Assistant 数据。

要连用户和这些数据一起删除（音乐库仍然保留）：

```sh
sudo hifimate uninstall --purge
```

不要不经卸载就直接删除这些目录，那样会留下服务、系统设置和改过的蓝牙名称。

## 6. 故障排查

**手机上看不到设备**

1. `sudo hifimate status` 确认所有服务都是 active。
2. 确认手机和主机在同一个网段，路由器没有开启「AP 隔离」或屏蔽 mDNS。
3. 主机有多个网卡时，AirPlay 只在默认路由的网卡上广播，确认手机能访问那个网卡的地址。

**能连接但没有声音**

1. 控制页的信号图里看当前输入是不是你正在播放的那一路，输出设备是否正确。
2. 看控制页「错误」日志，或者 `sudo hifimate logs hifimate`。常见原因是 DAC 不支持这个采样率
   （HiFiMate 不会偷偷重采样，而是报错并保持静音），或者 DAC 被别的程序占用（见下一条）。
3. 确认 DAC 本身的音量和输入选择。

**DAC 被占用**

主机上其他用户自己的 PipeWire 会打开同一块声卡，例如有人登录了桌面。专门放音乐的主机上，按 [1.4](#14-用户和权限) 停掉这些用户的音频服务；
有桌面的电脑上，HiFiMate 播放时不要在桌面上同时用这块声卡放声音。

**切换输入后原来的设备没有暂停**

有些发送端不接受远程暂停，例如不提供远程控制的 AirPlay 发送端、不响应暂停指令的蓝牙手机。这时 HiFiMate 照样切换到新的输入，
原来的设备需要手动暂停。DLNA 和 Music Assistant 总能暂停，但控制 App 的界面不一定马上同步。控制页的日志会记录每次暂停请求的结果。

**蓝牙名称没有变**

查看 `sudo hifimate logs bluetooth`。安装器已经把 `hifimate` 用户加入 `bluetooth` 组；改过组之后要重新安装一次
（`sudo hifimate install`），新的组才对服务生效。

**连不上共享文件夹**

1. `systemctl status smbd`（openSUSE、Fedora 上是 `smb`）确认 Samba 在运行；防火墙要放行 Samba（Fedora 上安装器已自动放行）。
2. 用户名是 `hifimate`，密码是控制页密码。密码不对时用 `sudo hifimate password` 重新设置。
3. Windows 提示「不能访问」时，确认没有关闭 SMB2/3（HiFiMate 不支持过时的 SMB1）。

**80 端口被占用，Music Assistant 起不来**

见 [Music Assistant 使用说明](music-assistant/README.md#更换网页端口)。

**安装器提示 PipeWire 或 WirePlumber 版本太旧**

见 [1.1 操作系统](#11-操作系统)。

## 7. 安装在主机上的内容

日常会用到的文件见 [README 的「安装后的文件」](README.md#安装后的文件)。完整列表：

| 位置 | 内容 |
|---|---|
| `/usr/local/bin/hifimate`、`/opt/pipx/` | `hifimate` 命令和它的 Python 环境 |
| `/opt/hifimate/` | 程序、服务定义、配置，以及你的 EQ、房间校正、证书、Music Assistant 的部署配置 |
| `/var/lib/hifimate/` | 用户 `hifimate` 的主目录：PipeWire、WirePlumber 的配置片段和 systemd 用户服务的链接（`.config/`），Music Assistant 的数据 |
| `/srv/hifimate/music/` | 音乐库 |
| `/etc/hifimate/install.json` | 记住的安装选项 |
| `/etc/systemd/system/user@<UID>.service.d/90-hifimate-rtprio.conf` | 实时调度上限 |
| `/etc/bluetooth/main.conf` 中带 HiFiMate 标记的两行 | 蓝牙音箱类别 |
| `/etc/samba/smb.conf` 末尾带 HiFiMate 标记的 `[Music]` 段、`/etc/avahi/services/hifimate-smb.service` | 音乐共享，以及让 Mac 在「网络」里看到它 |
| `/etc/pam.d/hifimate` | 控制页验证密码用的 PAM 设置（只有 pam_unix） |
| `/etc/ssh/sshd_config.d/hifimate.conf` | 禁止 `hifimate` 用户通过 SSH 登录 |
| `/var/lib/hifimate-realtime/`、`/var/lib/hifimate-bluetooth-speaker/` | 上面两项系统设置的归属记录，卸载时据此还原 |

安装器记录了自己写入的每个文件的哈希。你手动改过的 HiFiMate 程序文件，重新安装和卸载时都会拒绝覆盖或删除，并提示是哪个文件。

## 8. 开发者

### 8.1 从源码安装

两种方式都从源码目录运行，改代码后重新运行同一条命令即可部署（会重启所有服务）。

**系统方式**（与用户的安装相同，用专门的 `hifimate` 用户）：

```sh
git clone https://github.com/hifimate/hifimate.git
cd hifimate
sudo python3 -B -m hifimate.cli install --dry-run   # 只显示计划
sudo python3 -B -m hifimate.cli install --name 客厅
```

它把源码目录复制到 `/var/tmp` 交给 `hifimate` 用户，其余与 `sudo hifimate install` 完全相同。

**用户方式**（装在当前用户的 `~/hifimate`，不建用户、不共享；适合开发机或没有 root 的机器）：

```sh
scripts/install.sh --install-deps --enable --with-music-assistant --name 客厅          # 只显示计划
scripts/install.sh --apply --install-deps --enable --with-music-assistant --name 客厅  # 实际安装
```

`scripts/install.sh` 是实际的安装器，两种方式最终都调用它；不带 `--apply` 时只显示计划。
用户方式下 `--apply`、`--enable`、`--with-music-assistant`、`--install-deps` 都要显式给出，需要 sudo 的两项系统设置用
`--setup-realtime`、`--setup-bluetooth-speaker`，服务要开机运行还需 `loginctl enable-linger "$USER"`。
用户方式下控制页密码就是你自己的登录密码（PAM 服务 `hifimate`；没有 `/etc/pam.d/hifimate` 时用系统的 `other` 设置，
部分发行版的 `other` 一律拒绝，这时照 [3.4](#34-安装器做的系统设置) 写入那个文件）。
卸载用 `scripts/uninstall.sh --apply`。也可以用 `hifimate install`（不带 sudo）走同样的用户方式；
一键安装脚本用 `HIFIMATE_USER_MODE=1` 时也是用户方式。

只检查依赖：

```sh
python3 scripts/host_stack.py --check
scripts/install.sh --list-outputs
```

### 8.2 测试

```sh
python3 -m pip install -e '.[test,host]'   # 或者使用发行版的 python3-pytest 等软件包
python3 -B -W error -m pytest -q
node tests/ctrl_browser.mjs                 # 控制页的浏览器测试
node tests/spectrum_browser.mjs             # 频谱的浏览器测试
node tests/roomfit_browser.mjs              # 房间校正页的浏览器测试
```

测试不会碰本机的服务、声卡和 sudo：系统命令都被替换成假的，误调用会直接失败。
Music Assistant 音乐源的测试需要 Music Assistant 本身，在它的镜像里运行：`scripts/test_ma_providers.sh`。
对真实服务的冒烟测试在 `scripts/providers/*_live_smoke.py`。

推送前建议在干净的 Python 3.11 容器里跑一遍（CI 用 3.11 和 3.13），本机装过的东西有时会掩盖问题：

```sh
docker run --rm -v "$PWD":/src:ro python:3.11-bookworm sh -c '
  apt-get update -qq && apt-get install -y -qq nodejs git openssl iproute2 >/dev/null
  cp -r /src /w && cd /w && pip install -q -e ".[test,host]" && python -B -W error -m pytest -q'
```

在临时的 PipeWire 实例里检查采样率切换（不打开声卡）：

```sh
python3 scripts/check-format-stack.py
python3 scripts/check-format-stack.py --mpd
```

#### README 截图

README 里的截图（`assets/screenshots/`）来自一个演示服务：它运行真正的控制页和频谱服务，只是数据是虚构的——
一首虚构的曲目、示例 EQ、用 HiFiMate 自己的拟合代码算出来的示例房间校正，频谱分析的是一段合成音乐。
不会出声，不读本机的配置和曲目，密码是 `demo-pass`。界面改了以后重新生成：

```sh
python3 tools/screenshots/demo.py &        # http://127.0.0.1:5099/ctrl/；EQ 曲线要用已安装的 CamillaDSP（--camilladsp）
PLAYWRIGHT_CORE=…/node_modules/playwright-core CHROMIUM=…/chrome node tools/screenshots/shoot.mjs
```

`shoot.mjs` 需要 playwright-core 和一个 Chromium，图片由 Chromium 自己缩放并编码成 WebP。

### 8.3 调试仲裁服务

仲裁服务在 `/run/user/<UID>/hifimate/format.sock` 提供本地接口，只接受同一用户。系统安装时查看当前状态：

```sh
sudo runuser -u hifimate -- env XDG_RUNTIME_DIR=/run/user/$(id -u hifimate) \
  PYTHONPATH=/opt/hifimate/lib python3 -m hifimate.format_cli '{"action":"status"}'
```

返回里 `ready: true` 且 `effective` 不为空，表示整条链路已经核对过；`effective` 给出图时钟、DSP 和 DAC 的实际采样率。
`error` 是失败原因。以 `hifimate` 用户运行任何命令（例如 `systemctl --user`、`pactl`）都用上面的 `runuser … env XDG_RUNTIME_DIR=…` 前缀。

### 8.4 打包和发布

`pyproject.toml` 用 hatchling 构建；wheel 里除了 `hifimate` 包，还在 `hifimate/_dist/` 下带着安装器需要的全部文件。
版本号只写在 `hifimate/__init__.py`。发布步骤见 [packaging/RELEASING.md](packaging/RELEASING.md)。

本地构建并检查 wheel：

```sh
python3 -m pip wheel --no-deps -w dist .
python3 -B packaging/check_wheel.py dist/hifimate-*.whl
```

代码结构和工作原理见 [ARCHITECTURE.md](docs/ARCHITECTURE.md)。
