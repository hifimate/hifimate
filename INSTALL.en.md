[中文](INSTALL.md) | **English**

# Installation and Maintenance

This guide goes in order: check the prerequisites, install, then do the first-time setup. Routine maintenance, uninstalling and troubleshooting come after that.
For where each file ends up after installation, see [Installed files in the README](README.en.md#installed-files).
Instructions for developing, testing and releasing from source are collected in the last chapter, "[For developers](#8-for-developers)".

## 1. Prerequisites

Before installing, check each of the following.

### 1.1 Operating system

PipeWire 1.0 or later and WirePlumber 0.5 or later are required. The systems in the table below have all had their dependencies installed and checked in the official containers;
the complete playback chain has been verified on real hardware on Debian 13.

| System | Support |
|---|---|
| Debian 13 (trixie) and later | Supported (main development platform). For upmpdcli, add the [upstream repository](https://www.lesbonscomptes.com/upmpdcli/pages/downloads.html) first |
| Ubuntu 26.04 LTS and later | Supported. For upmpdcli, add the PPA `ppa:jean-francois-dockes/upnpp1` first |
| Ubuntu 24.04 LTS | Supported, but PipeWire must be upgraded first (see below) |
| Fedora 43 and later | Supported. For upmpdcli, use the [RPM provided upstream](https://www.lesbonscomptes.com/upmpdcli/pages/downloads.html) |
| Arch Linux | Supported; all packages are in the official repositories |
| openSUSE Tumbleweed | Supported; all packages are in the official repositories |
| Debian 12 | Not supported: PipeWire 0.3 and WirePlumber 0.4 are too old, please upgrade to Debian 13 |
| RHEL, CentOS Stream, Rocky, AlmaLinux | Not supported for now (see below) |

**Ubuntu 24.04 LTS** ships WirePlumber 0.4, which cannot read HiFiMate's configuration. First upgrade to
PipeWire 1.0.7 and WirePlumber 0.5.2 using the PipeWire upstream maintainer's PPAs, then install HiFiMate:

```sh
sudo add-apt-repository ppa:pipewire-debian/pipewire-upstream
sudo add-apt-repository ppa:pipewire-debian/wireplumber-upstream
sudo add-apt-repository ppa:jean-francois-dockes/upnpp1      # upmpdcli
sudo apt update && sudo apt full-upgrade
systemctl --user restart pipewire pipewire-pulse wireplumber  # or log in again
```

These two PPAs replace the audio core components of the whole machine and affect every program on it that makes sound. For a host dedicated to playing music, we recommend installing Ubuntu 26.04 LTS or Debian 13 instead.

**RHEL, CentOS Stream, Rocky and AlmaLinux are not supported for now**, because three required components are missing from the official repositories and EPEL of these systems:
MPD (both the DLNA and Music Assistant inputs depend on it), upmpdcli (DLNA receiving) and python3-dbus-next (Bluetooth).
Supporting them would mean building and maintaining these packages ourselves for the long term, or putting them in a container (the sound card, PipeWire and Bluetooth would all have to pass through into the container).
Both routes are costly to maintain and verify. If you want a Red Hat-family system, use Fedora instead.

### 1.2 Hardware

- The CPU must be x86_64 or aarch64 (CamillaDSP officially provides PulseAudio builds only for these two).
- A USB DAC that supports stereo and a fixed list of sample rates in a 32-bit container (S32_LE). Common USB audio DACs generally meet this, and the installer checks it.
  Without a DAC you can use the host's built-in analog output; HDMI, DisplayPort and S/PDIF are not supported.
- For Bluetooth you need a Bluetooth adapter (built into the host or USB).
- The sound card is used exclusively by HiFiMate. The PipeWire of other users on the host (for example someone logged in to the desktop) will also use this card; see [1.4](#14-users-and-permissions).

### 1.3 Software

- **System packages**: PipeWire, WirePlumber, BlueZ, MPD, upmpdcli, Shairport Sync, Avahi, plus Python 3.10+ and
  NumPy, PyYAML, websocket-client and dbus-next. The installer installs them automatically with the system package manager (apt, dnf, pacman, zypper).
  Only the upmpdcli repository mentioned in the table above has to be added by you beforehand; if it is missing, the installer tells you where to get it.
- **CamillaDSP 4.1.3**: the installer downloads the official release from GitHub and verifies it against a pinned SHA256; you do not need to build it.
- **Samba**: for sharing the music library. The installer installs it automatically (add `--without-share` if you do not want to share).
- **pipx**: with the one-line install, the install script installs it automatically if the system does not have it.
- **Docker Engine and Compose v2**: needed for Music Assistant. The installer does not install them; please install and start them first following the
  [official Docker instructions](https://docs.docker.com/engine/install/).

### 1.4 Users and permissions

- Installation needs root: run the install command with `sudo`. The installer creates a dedicated user `hifimate` to run HiFiMate
  (it cannot log in; home directory `/var/lib/hifimate`), adds it to the `audio`, `bluetooth` and `docker` groups, and makes it start at boot without anyone logging in.
  After that, daily operation no longer needs sudo; only the management command `sudo hifimate …` itself uses sudo.
- The Linux password of the `hifimate` user is the control page password, and also the music share password. The account's shell is nologin, and the installer also forbids it from logging in over SSH,
  so this password can only be used to log in to the control page and to access the share.
- The installer sets up two system settings, the realtime scheduling limit and the Bluetooth speaker class, directly for the `hifimate` user (see [3.4](#34-system-settings-made-by-the-installer)), and restores them on uninstall.
- **Ideally this host is dedicated to playing music.** When other users log in to the desktop, their own PipeWire also uses the same sound card
  and may compete with HiFiMate for it. On a host managed only over SSH, you can stop the administrator account's own audio services
  so that the sound card belongs to HiFiMate alone (run as the administrator account; to restore later, replace `mask` with `unmask`):

  ```sh
  systemctl --user mask --now pipewire.socket pipewire-pulse.socket pipewire.service pipewire-pulse.service wireplumber.service
  ```

### 1.5 Network and ports

The host and your phone or computer must be on the same local network, and the network must allow multicast: AirPlay uses mDNS and DLNA uses SSDP for device discovery.

| Port | Purpose | Open to the LAN |
|---|---|---|
| 80 | Music Assistant and the control page (`/ctrl/`) | Yes |
| 443 | Room correction page (HTTPS) | Yes |
| 8095, 8097 | Music Assistant web interface and audio stream | Yes (needed by Music Assistant itself) |
| AirPlay and DLNA ports | Chosen automatically by Shairport Sync and upmpdcli | Yes |
| 6600, 6601, 5008, 5009, 3003, 3004, 1234 | Internal services | No, listening on 127.0.0.1 only |

If port 80 is taken you can change it; see the [Music Assistant guide](music-assistant/README.en.md#changing-the-web-port).

## 2. Installation

### 2.1 One-line install

```sh
curl -fsSL https://github.com/hifimate/hifimate/releases/latest/download/install.sh | sudo sh
```

This command does the following in order:

1. If pipx is missing, installs it with the system package manager.
2. Installs the `hifimate` command with pipx (`/usr/local/bin/hifimate`, with the packages in `/opt/pipx`).
3. Runs `hifimate install`:
   1. installs the system packages (including Samba) and downloads CamillaDSP;
   2. creates the user `hifimate` and prepares `/opt/hifimate` (program and settings) and `/srv/hifimate/music` (music library);
   3. sets the realtime scheduling limit and the Bluetooth speaker class;
   4. starts all services as the `hifimate` user and deploys Music Assistant;
   5. shares the music library as `Music`.

During installation it asks for:

1. **Control page password**: at least 8 characters. It is the Linux password of the `hifimate` user; the system (PAM) verifies it at control page login, and HiFiMate itself does not store passwords;
   the music share (user `hifimate`) uses the same password.
2. **Device name**, for example `客厅` ("living room"). On your phone you will see `客厅 AirPlay`, `客厅 DLNA` and `客厅 Bluetooth`.
   The name may contain Chinese characters, spaces, letters, digits and `-_.()`, up to 42 bytes (about 14 Chinese characters).
3. **Output device**: chosen automatically if there is only one compatible DAC; if there are several, you are asked to choose.
4. **Music Assistant administrator**: a user name and password for logging in to the Music Assistant web page. This step cannot be skipped,
   because the HiFiMate control page's control of Music Assistant depends on it.

To give the installer options, replace `| sudo sh` with `| sudo sh -s -- options…`, for example:

```sh
curl -fsSL https://github.com/hifimate/hifimate/releases/latest/download/install.sh | sudo sh -s -- --name 客厅 --without-music-assistant
```

If pipx is already installed, you can also do it in two steps: `sudo PIPX_HOME=/opt/pipx PIPX_BIN_DIR=/usr/local/bin pipx install hifimate`,
then `sudo hifimate install options…`.

### 2.2 Install options

| Option | Effect |
|---|---|
| `--name NAME` | Device name; when reinstalling, omit it to keep the existing name |
| `--output-device hw:CARD=CARD_ID,DEV=DEVICE_NUMBER` | Choose the output device (to list available devices, see [4.4](#44-change-the-output-device)) |
| `--choose-output` | List the available output devices and let you choose |
| `--music-directory DIR` | Location of the music library, default `/srv/hifimate/music`; the owner of an existing directory is not changed, and the `hifimate` user must be able to read and write it |
| `--without-share` | Do not share the music library on the LAN |
| `--without-music-assistant` | Do not deploy Music Assistant (so Docker is not needed either) |
| `--without-ma-provider bilibili` or `migu` | Do not install one of the bundled music sources; `--without-ma-providers` skips both |
| `--realtime off` | Do not use realtime scheduling |
| `--no-install-deps` | Do not call the package manager; you install the system packages yourself |
| `--dry-run` | Only show what would be done, change nothing |
| `--non-interactive` | Unattended, ask no questions (see 2.3) |

The options are recorded in `/etc/hifimate/install.json` (without passwords). When you run `sudo hifimate install` again, you only need to give the options you want to change; the others carry over from last time.
`sudo hifimate upgrade` also reuses these options. To install exactly with the options you give now, add `--reset-options`.
If you did not install Music Assistant before and now want it, use `--with-music-assistant`; if there was no share before and you now want one, use `--with-share`.

### 2.3 Unattended install

`--non-interactive` asks no questions. Everything that would have been asked must be supplied beforehand; if something is missing, it exits with an error before changing anything.
When run from a pipe with no terminal, it is treated as unattended automatically.

Passwords must not go in command-line arguments (arguments are visible to every user on the machine); supply them through a file readable only by root, or through environment variables:

| What | Via a file | Or via an environment variable |
|---|---|---|
| Control page (and music share) password | `--ctrl-password-file FILE` | `HIFIMATE_CTRL_PASSWORD` |
| Music Assistant administrator user name | `--ma-admin-user NAME` | `HIFIMATE_MA_ADMIN_USER` |
| Music Assistant administrator password | `--ma-admin-password-file FILE` | `HIFIMATE_MA_ADMIN_PASSWORD` |

With a file (recommended):

```sh
sudo install -m 600 /dev/null /root/hifimate.pw && sudo nano /root/hifimate.pw     # write the password
curl -fsSL https://github.com/hifimate/hifimate/releases/latest/download/install.sh | sudo sh -s -- \
  --name 客厅 --output-device hw:CARD=DAC,DEV=0 \
  --ctrl-password-file /root/hifimate.pw --ma-admin-user admin --ma-admin-password-file /root/hifimate.pw
```

With environment variables, `sudo` drops them by default, so pass them explicitly with `--preserve-env`. Do not write `sudo VAR=… sh`; that would put the password in the process list:

```sh
export HIFIMATE_CTRL_PASSWORD='…' HIFIMATE_MA_ADMIN_USER=admin HIFIMATE_MA_ADMIN_PASSWORD='…'
curl -fsSL https://github.com/hifimate/hifimate/releases/latest/download/install.sh | \
  sudo --preserve-env=HIFIMATE_CTRL_PASSWORD,HIFIMATE_MA_ADMIN_USER,HIFIMATE_MA_ADMIN_PASSWORD sh -s -- --name 客厅
```

Notes:

- A password is 8–1024 characters and must not contain newlines; one trailing newline at the end of a file is removed.
- When reinstalling, giving a control page password replaces the old one (the Linux password of the `hifimate` user and the share password change together); omit it to keep the old one.
- If Music Assistant already has users, the administrator you give is ignored and its password is not changed.

## 3. Post-install setup

### 3.1 Open the control page

Open `http://<host IP>/ctrl/` in a browser. You can find the host IP on the host with `hostname -I`; from a phone or another computer you cannot use `127.0.0.1`.
Viewing the status needs no login; to operate anything, log in with the control page password. A login is valid for 30 days.

### 3.2 Set up Music Assistant

HiFiMate is already set as a Music Assistant player (`<name> MA`), so on first use you only need to add music sources; for the steps see the
[Music Assistant guide](music-assistant/README.en.md#first-time-setup).

### 3.3 Bluetooth pairing

On the control page's Bluetooth card, click the pairing button (「配对 120 s」), or run `sudo hifimate pair` on the host (`--seconds` can set 10–600 seconds),
then choose `<name> Bluetooth` in your phone's Bluetooth settings. The first device to pair successfully becomes trusted, and the window closes right away. After that, the phone simply connects; no window is needed.
If a phone was paired before, was removed on the phone and is then paired again, the steps are the same.

Make sure the system's Bluetooth and Docker services start at boot: `systemctl is-enabled bluetooth docker`.

### 3.4 System settings made by the installer

The installer sets up the following two items as root, and restores them automatically on uninstall:

- **Realtime scheduling limit**: lets the CamillaDSP, MPD, Shairport Sync and PipeWire audio threads of the `hifimate` user run with low-priority realtime scheduling,
  which reduces pops and clicks under high load or with a realtime kernel (PREEMPT_RT). It is written to `/etc/systemd/system/user@<UID>.service.d/90-hifimate-rtprio.conf`.
  If you do not need it, add `--realtime off` when installing.
- **Bluetooth speaker class**: adds two lines marked as HiFiMate's to `/etc/bluetooth/main.conf`, so that phones show the host with a speaker icon
  and phones that were paired before are allowed to pair again. The Bluetooth service is restarted once. If `main.conf` already has a Class that you set yourself, it is not changed.

Control page password: the installer writes `/etc/pam.d/hifimate` (which verifies passwords with pam_unix only), sets the Linux password of the `hifimate` user with `chpasswd`,
and writes `/etc/ssh/sshd_config.d/hifimate.conf` to forbid this user from logging in over SSH. These two files are deleted together with the user by `uninstall --purge`.

In addition, sharing the music library appends a marked `[Music]` section to the end of `/etc/samba/smb.conf` and sets a Samba password for the `hifimate` user;
nothing else in `smb.conf` is touched, and if there is already another `[Music]` share, it is not changed.

### 3.5 Keep the host always on (optional)

If the host uses Wi‑Fi or is a laptop, the settings below prevent network drops and sleep. They are host-wide policy, so HiFiMate does not set them automatically and does not restore them on uninstall.

**Turn off Wi‑Fi power saving** (only for hosts that manage Wi‑Fi with NetworkManager):

```sh
sudo tee /etc/NetworkManager/conf.d/20-hifimate-wifi-powersave.conf >/dev/null <<'EOF'
[connection]
wifi.powersave = 2
EOF
sudo nmcli general reload conf
```

It takes effect after Wi‑Fi reconnects; check with `iw dev <interface> get power_save`. To restore: delete this file, then run `sudo nmcli general reload conf`.

**Do not sleep when the lid is closed** (laptops):

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

Restarting `systemd-logind` may log out the current desktop session, so do it when the desktop is not in use. To restore: delete this file, then restart `systemd-logind`.

**Forbid sleep entirely** (when you are sure this host never needs any kind of sleep):

```sh
sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target suspend-then-hibernate.target
```

To restore, replace `mask` with `unmask`.

## 4. Routine maintenance

### 4.1 Check status

```sh
sudo hifimate status
```

It shows the installed version, the output device, the music library, and whether each service and the Music Assistant container are healthy. The control page's services section (「服务」) shows the same information and the logs.
To view logs:

```sh
sudo hifimate logs              # last 100 lines of all services
sudo hifimate logs airplay -f   # AirPlay only, following the output; can also be dlna, bluetooth, camilladsp, mpd, ctrl, etc.
```

For what each service does, see [ARCHITECTURE.md](docs/ARCHITECTURE.en.md#2-components).

### 4.2 Upgrade

```sh
sudo hifimate upgrade                 # upgrade to the latest version
sudo hifimate upgrade --version 0.2.0 # upgrade or go back to a specific version
```

An upgrade reuses the options from the last install and restarts all services, which interrupts any music that is playing. Your password, EQ, room correction, music library and Music Assistant data are all kept.

### 4.3 Your own EQ

Edit the EQ on the EQ page (`http://<host IP>/ctrl/eq/`); see the [README](README.en.md#your-own-eq). It is saved in `/opt/hifimate/config/dsp-filters.yml`,
which you can also edit directly with `sudo nano`. The page and manual editing are compatible with each other; if the page finds on save that the file was changed in the meantime, it warns you instead of overwriting. The rules:

- Only `Filter` steps are allowed; the filter types you can use are Biquad, BiquadCombo, Gain, Delay, DiffEq and Conv. Volume and Loudness are not allowed.
- Channels are written as `[0]` (left), `[1]` (right) or `[0, 1]`; every filter you define must be used in the `pipeline`.
- The processing order is: headroom → your EQ → room correction. All sample rates use the same EQ.
- After editing by hand, run `sudo hifimate reload-filters` (on the page, just click the apply button (「应用」)). If the syntax is wrong it explains why; playback carries on as normal, just without your EQ.

### 4.4 Change the output device

Switch on the control page's output card, or on the host:

```sh
sudo hifimate output                               # list available output devices
sudo hifimate output --choose                      # choose and switch
sudo hifimate output --device hw:CARD=DAC,DEV=0    # switch directly to the given device
```

Switching interrupts any music that is playing. Names in the list start with "USB" (「USB」) or "built-in" (「本机」), followed by a device ID of the form `hw:CARD=…,DEV=…`,
and the ID can also be used with the install option `--output-device`.

When the output device is `auto`: if there is exactly one compatible USB DAC it is used; with no USB DAC, the only built-in analog output is used;
with several, you must specify one explicitly.

### 4.5 Change the name and password

- To change the name: `sudo hifimate install --name NEW_NAME`.
- To change the control page password: `sudo hifimate password`. It changes the Linux password of the `hifimate` user and the music share password, and makes every logged-in browser log in again.
- Change the Music Assistant password on the Music Assistant web page.

### 4.6 Other settings

Change them on the settings page (the gear at the top right of the control page):

| Setting | Default | Effect |
|---|---|---|
| Fade-in (「淡入」) | 400 ms | When playback starts, rises from silence to normal volume, 20–2000 ms |
| Release DAC (「释放 DAC」) | 5 minutes | How long after the sound stops before the senders are paused and the DAC is closed; can be set to never release |
| Allowed sample rates (「允许的采样率」) | All the DAC supports | Individual rates can be excluded; a track at an excluded rate gives an error and stays silent, it is not resampled |

After saving, the arbiter service restarts, and music that is playing is interrupted for a few seconds. These settings are stored in `/opt/hifimate/config/settings.json` and are kept across reinstalls and upgrades.

`/opt/hifimate/config/hifimate.conf` holds defaults managed by the installer and recorded in the install manifest; **do not edit it by hand**: if you do, the next install or upgrade refuses to continue.
To change the settings above from the host, edit `settings.json` (keys `fade_ms`, `idle_timeout_s`, `rates`), then run `sudo hifimate restart`.

## 5. Uninstall

```sh
sudo hifimate uninstall --dry-run   # first see what would be removed
sudo hifimate uninstall
sudo PIPX_HOME=/opt/pipx PIPX_BIN_DIR=/usr/local/bin pipx uninstall hifimate   # finally remove the hifimate command itself
```

Uninstalling stops and removes HiFiMate's services and program, removes the Music Assistant container, restores the Bluetooth name, and undoes the realtime scheduling limit, the Bluetooth speaker class and the music share.
The following are kept and will be used again on the next install:

- System packages (PipeWire, BlueZ, MPD, Samba and so on), which may have other uses;
- The music library `/srv/hifimate/music`;
- The user `hifimate` (and its password), and the data that belongs to you: in `/opt/hifimate`, the EQ, room correction and measurement records, local certificates,
  Music Assistant settings and separately installed music sources; in `/var/lib/hifimate`, the Music Assistant data.

To delete the user together with this data (the music library is still kept):

```sh
sudo hifimate uninstall --purge
```

Do not delete these directories directly without uninstalling; that would leave behind services, system settings and a changed Bluetooth name.

## 6. Troubleshooting

**The device does not show up on the phone**

1. Run `sudo hifimate status` and confirm all services are active.
2. Confirm the phone and the host are on the same network segment, and that the router has not enabled "AP isolation" (「AP 隔离」) or blocked mDNS.
3. When the host has several network interfaces, AirPlay only advertises on the interface of the default route; confirm the phone can reach that interface's address.

**It connects but there is no sound**

1. On the control page's signal graph, check whether the current input is the one you are playing, and whether the output device is correct.
2. Look at the control page's error log (「错误」), or run `sudo hifimate logs hifimate`. A common cause is that the DAC does not support the sample rate
   (HiFiMate never resamples quietly; it reports an error and stays silent), or that the DAC is taken by another program (see the next item).
3. Check the DAC's own volume and input selection.

**The DAC is in use by something else**

The PipeWire of other users on the host opens the same sound card, for example when someone is logged in to the desktop. On a host dedicated to music, stop those users' audio services as described in [1.4](#14-users-and-permissions);
on a computer with a desktop, do not play sound through this card from the desktop while HiFiMate is playing.

**The previous device did not pause after switching input**

Some senders do not accept a remote pause, for example AirPlay senders that offer no remote control, or Bluetooth phones that do not respond to the pause command. HiFiMate still switches to the new input,
and the previous device has to be paused by hand. DLNA and Music Assistant can always be paused, but the control app's interface may not update right away. The control page's log records the result of every pause request.

**The Bluetooth name did not change**

Run `sudo hifimate logs bluetooth`. The installer has already added the `hifimate` user to the `bluetooth` group; after a group change you must install once more
(`sudo hifimate install`) for the new group to take effect for the services.

**Cannot connect to the shared folder**

1. Run `systemctl status smbd` (`smb` on openSUSE and Fedora) to confirm Samba is running; the firewall must allow Samba (on Fedora the installer already allows it).
2. The user name is `hifimate` and the password is the control page password. If the password is wrong, reset it with `sudo hifimate password`.
3. If Windows says it "cannot access" (「不能访问」) the share, make sure SMB2/3 has not been turned off (HiFiMate does not support the obsolete SMB1).

**Port 80 is taken and Music Assistant will not start**

See the [Music Assistant guide](music-assistant/README.en.md#changing-the-web-port).

**The installer says the PipeWire or WirePlumber version is too old**

See [1.1 Operating system](#11-operating-system).

## 7. What is installed on the host

For the files you will use day to day, see [Installed files in the README](README.en.md#installed-files). The full list:

| Location | Contents |
|---|---|
| `/usr/local/bin/hifimate`, `/opt/pipx/` | The `hifimate` command and its Python environment |
| `/opt/hifimate/` | Program, service definitions, configuration, and your EQ, room correction, certificates and the Music Assistant deployment configuration |
| `/var/lib/hifimate/` | Home directory of the user `hifimate`: PipeWire and WirePlumber configuration snippets and links to the systemd user services (`.config/`), and the Music Assistant data |
| `/srv/hifimate/music/` | Music library |
| `/etc/hifimate/install.json` | Remembered install options |
| `/etc/systemd/system/user@<UID>.service.d/90-hifimate-rtprio.conf` | Realtime scheduling limit |
| The two lines marked as HiFiMate's in `/etc/bluetooth/main.conf` | Bluetooth speaker class |
| The `[Music]` section marked as HiFiMate's at the end of `/etc/samba/smb.conf`, and `/etc/avahi/services/hifimate-smb.service` | The music share, and making it visible to Macs under "Network" (「网络」) |
| `/etc/pam.d/hifimate` | PAM configuration the control page uses to verify passwords (pam_unix only) |
| `/etc/ssh/sshd_config.d/hifimate.conf` | Forbids the `hifimate` user from logging in over SSH |
| `/var/lib/hifimate-realtime/`, `/var/lib/hifimate-bluetooth-speaker/` | Ownership records for the two system settings above, used to restore them on uninstall |

The installer records a hash of every file it writes. If you have edited a HiFiMate program file by hand, reinstalling and uninstalling both refuse to overwrite or delete it, and tell you which file it is.

## 8. For developers

### 8.1 Install from source

Both ways are run from the source directory; after changing code, run the same command again to deploy (all services are restarted).

**System mode** (the same as a user's install, with the dedicated `hifimate` user):

```sh
git clone https://github.com/hifimate/hifimate.git
cd hifimate
sudo python3 -B -m hifimate.cli install --dry-run   # only show the plan
sudo python3 -B -m hifimate.cli install --name 客厅
```

It copies the source directory to `/var/tmp` and hands it to the `hifimate` user; everything else is exactly the same as `sudo hifimate install`.

**User mode** (installs into the current user's `~/hifimate`, creates no user, shares nothing; suited to a development machine or a machine without root):

```sh
scripts/install.sh --install-deps --enable --with-music-assistant --name 客厅          # only show the plan
scripts/install.sh --apply --install-deps --enable --with-music-assistant --name 客厅  # actually install
```

`scripts/install.sh` is the real installer, and both modes end up calling it; without `--apply` it only shows the plan.
In user mode, `--apply`, `--enable`, `--with-music-assistant` and `--install-deps` must all be given explicitly; the two system settings that need sudo use
`--setup-realtime` and `--setup-bluetooth-speaker`, and for the services to run at boot you also need `loginctl enable-linger "$USER"`.
In user mode the control page password is your own login password (PAM service `hifimate`; without `/etc/pam.d/hifimate` the system's `other` configuration is used,
and on some distributions `other` rejects everything, in which case write that file as described in [3.4](#34-system-settings-made-by-the-installer)).
To uninstall, use `scripts/uninstall.sh --apply`. You can also run `hifimate install` (without sudo) for the same user mode;
the one-line install script is also in user mode when you use `HIFIMATE_USER_MODE=1`.

To check dependencies only:

```sh
python3 scripts/host_stack.py --check
scripts/install.sh --list-outputs
```

### 8.2 Tests

```sh
python3 -m pip install -e '.[test,host]'   # or use the distribution's python3-pytest and similar packages
python3 -B -W error -m pytest -q
node tests/ctrl_browser.mjs                 # browser tests for the control page
node tests/spectrum_browser.mjs             # browser tests for the spectrum
node tests/roomfit_browser.mjs              # browser tests for the room correction page
```

The tests do not touch the local services, sound card or sudo: system commands are replaced with fakes, and a stray call fails immediately.
The tests for the Music Assistant music sources need Music Assistant itself and run inside its image: `scripts/test_ma_providers.sh`.
Smoke tests against the real services are in `scripts/providers/*_live_smoke.py`.

Before pushing, we recommend running everything once in a clean Python 3.11 container (CI uses 3.11 and 3.13); what is installed on your machine can sometimes hide problems:

```sh
docker run --rm -v "$PWD":/src:ro python:3.11-bookworm sh -c '
  apt-get update -qq && apt-get install -y -qq nodejs git openssl iproute2 >/dev/null
  cp -r /src /w && cd /w && pip install -q -e ".[test,host]" && python -B -W error -m pytest -q'
```

To check sample rate switching in a temporary PipeWire instance (without opening the sound card):

```sh
python3 scripts/check-format-stack.py
python3 scripts/check-format-stack.py --mpd
```

#### README screenshots

The screenshots in the README (`assets/screenshots/`) come from a demo server: it runs the real control page and spectrum service, only the data is made up:
a made-up track, an example EQ, an example room correction computed with HiFiMate's own fitting code, and a spectrum analysis of a piece of synthetic music.
It makes no sound, does not read the local configuration or tracks, and the password is `demo-pass`. Regenerate them after the interface changes:

```sh
python3 tools/screenshots/demo.py &        # http://127.0.0.1:5099/ctrl/; the EQ curve needs the installed CamillaDSP (--camilladsp)
PLAYWRIGHT_CORE=…/node_modules/playwright-core CHROMIUM=…/chrome node tools/screenshots/shoot.mjs
```

`shoot.mjs` needs playwright-core and a Chromium; the images are scaled and encoded to WebP by Chromium itself.

### 8.3 Debugging the arbiter service

The arbiter service offers a local interface at `/run/user/<UID>/hifimate/format.sock`, which only accepts the same user. For a system install, view the current state with:

```sh
sudo runuser -u hifimate -- env XDG_RUNTIME_DIR=/run/user/$(id -u hifimate) \
  PYTHONPATH=/opt/hifimate/lib python3 -m hifimate.format_cli '{"action":"status"}'
```

In the response, `ready: true` with a non-empty `effective` means the whole chain has been checked; `effective` gives the actual sample rates of the graph clock, the DSP and the DAC.
`error` is the reason for a failure. To run any command as the `hifimate` user (for example `systemctl --user` or `pactl`), use the `runuser … env XDG_RUNTIME_DIR=…` prefix above.

### 8.4 Packaging and release

`pyproject.toml` builds with hatchling; besides the `hifimate` package, the wheel carries all the files the installer needs under `hifimate/_dist/`.
The version number is written only in `hifimate/__init__.py`. For the release steps, see [packaging/RELEASING.md](packaging/RELEASING.md).

To build and check the wheel locally:

```sh
python3 -m pip wheel --no-deps -w dist .
python3 -B packaging/check_wheel.py dist/hifimate-*.whl
```

For the code structure and how it works, see [ARCHITECTURE.md](docs/ARCHITECTURE.en.md).
