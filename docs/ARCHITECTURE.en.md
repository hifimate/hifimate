[中文](ARCHITECTURE.md) | **English**

# Architecture and Implementation

This document explains how HiFiMate works and how it is implemented. It is written for readers who want to understand the internals or contribute. For usage, see the [README](../README.en.md); for installation and maintenance, see [INSTALL.en.md](../INSTALL.en.md).

## 1. Overview

```text
   DLNA controller → upmpdcli → MPD :6600 ──→ dlna_in ─────────────────┐
Music Assistant → MPD Players → MPD :6601 ──→ music_assistant_dlna_in ─┤
          AirPlay sender → Shairport Sync ──→ airplay_in ──────────────┤
              phone → BlueZ & WirePlumber ──→ bluez_input.* ───────────┤
                                                                       ↓
     lane chosen by the arbitration service → CamillaDSP → ALSA hw → DAC
```

Design principles:

- **Reuse mature software**: Shairport Sync, upmpdcli, MPD, and BlueZ receive the protocols; PipeWire provides the audio graph; the official CamillaDSP does the DSP; Music Assistant provides the library. HiFiMate's own code only coordinates: it decides who is playing, configures the sample rate, and provides the control page and the spectrum.
- **No mixing**: each input is its own PipeWire node (a lane), and the DSP captures only the one lane that is currently granted.
- **Only explicit playback actions count**: playback rights come from the protocol's "start playing" event, never inferred from "data is arriving" or "a device is connected".
- **Mute rather than play wrong sound**: if the sample rate, the device capabilities, or the readback of the signal path disagree anywhere, HiFiMate mutes and reports. It never silently resamples or degrades.
- **Do not modify the system**: all services are systemd user services of the dedicated user `hifimate`. Settings that need root (the realtime scheduling limit, the Bluetooth speaker device class, the Samba share) are made only at install time, have ownership records, and are restored on uninstall.

## 2. Components

### 2.1 User services

All services are systemd user services of the run user (`hifimate` in a system install). The audio graph is that user's own PipeWire and WirePlumber.

| Service | Role |
|---|---|
| `hifimate.service` | Arbitration service (`format_daemon.py`): receives the playback events of each protocol, decides playback rights, configures the sample rate and the DSP |
| `hifimate-camilladsp.service` | CamillaDSP, started with `--wait --no_config`; does not open the sound card until granted |
| `hifimate-mpd.service` | MPD for DLNA (127.0.0.1:6600) |
| `hifimate-music-mpd.service` | MPD for Music Assistant (127.0.0.1:6601) |
| `hifimate-dlna.service` | upmpdcli; publishes a DLNA renderer to the LAN and controls MPD 6600 |
| `hifimate-airplay.service` | Shairport Sync |
| `hifimate-airplay-net.service` | Follows the network interface of the default route and restarts Shairport when it changes (see 5.4) |
| `hifimate-bluetooth.service` | Maintains the Bluetooth adapter name and keeps the adapter non-discoverable outside the pairing window |
| `hifimate-spectrum.service` | Spectrum service (127.0.0.1:5008), see [SPECTRUM.md](SPECTRUM.en.md) |
| `hifimate-ctrl.service` | Control page (127.0.0.1:5009), see section 9 |

CamillaDSP, the two MPD instances, and Shairport are started through `bin/hifimate-rt`, which can optionally request realtime scheduling (section 13).

### 2.2 Code modules

| Module | Responsibility |
|---|---|
| `receiver_events.py` | Collects the playback events of each protocol, binds the single receiving stream, session isolation (fencing) |
| `mpd.py` | Subscribes to `idle player` of both MPD instances and produces play/stop edges |
| `airplay.py` | Parses Shairport's metadata pipe and produces AirPlay sessions |
| `bluetooth.py` | Watches state edges of the BlueZ MediaPlayer |
| `formats.py` | Format transaction: prepare a candidate, verify, commit, fade in, mute on failure |
| `remote_pause.py` | Makes the sender that was switched away from pause, and reads the result back |
| `pw_events.py` | Filters PipeWire events and ignores changes caused by the probing tools themselves |
| `format_daemon.py` | Local IPC, subscription lifecycle, work queue, systemd readiness notification |
| `runtime_paths.py` | Ownership checks and crash recovery for runtime directories, locks, sockets, and FIFOs |
| `output_devices.py` | Detects usable USB and onboard analog outputs; the output switch transaction |
| `identity.py`, `bluetooth_alias.py`, `bluetooth_pair.py` | Device name, setting and restoring the Bluetooth name, the pairing window |
| `airplay_net.py` | Chooses the network interface Shairport advertises on, by the default route |
| `spectrum.py`, `dsp_model.py`, `spectrum_proxy.py` | Spectrum |
| `ctrl.py`, `ctrl_state.py`, `ctrl_auth.py`, `nowplaying.py`, `ma_api.py`, `lyrics.py` | Control page, now playing, lyrics |
| `roomfit.py`, `room_eq.py`, `tls.py` | Room measurement, correction filters, local certificates |
| `user_filters.py` | Checking and merging the personal filter file |
| `realtime.py` | Small wrapper that requests realtime scheduling before startup |
| `cli.py` | The `hifimate` command |
| `core.py` | Pure in-memory model of the playback-rights policy, used by tests |

### 2.3 Ports

| Port | Listens on | Purpose |
|---|---|---|
| 80, 443 | All interfaces | nginx: the MA web UI, `/ctrl/`, `/_hifimate/`, room correction (HTTPS) |
| 8095, 8097 | All interfaces | MA web API, MA audio streams |
| 6600, 6601 | 127.0.0.1 | The two MPD instances |
| 5008 | 127.0.0.1 | Spectrum SSE and frontend files |
| 5009 | 127.0.0.1 | Control page |
| 3003, 3004 | 127.0.0.1 | NetEase Cloud Music (网易云音乐) and Migu (咪咕) API services |
| 1234 | 127.0.0.1 | CamillaDSP websocket |

Binding to loopback only prevents direct access from the LAN; it is not a permission boundary between local users.

## 3. Playback Rights

### 3.1 Rules

- The most recent valid playback action gets the playback right; only one input is output at a time.
- A valid playback action is the protocol's start/resume edge: the MPD state changing from non-play to play; AirPlay `pbeg`/`pres`; the BlueZ MediaPlayer Status becoming playing.
- Connecting a device, the initial state snapshot, a repeated playing, and PCM that keeps arriving do not create a playback right. When the previous sender failed to pause and is still sending data, it also cannot win the playback right back on that basis.
- After the current input stops, the system goes idle and **does not automatically resume** the input that was switched away from; that input needs a new playback action.
- AirPlay `prsm`/`pfls` do not preempt. The only exception is a `prsm` on a connection that HiFiMate itself paused through DACP (the user resumes on the sender). A `pbeg` after a new connection (a new `clip/daid/acre`) is always a new session, to cover the case where the sender or Shairport crashed without a `pend`.
- DLNA's playback right comes from MPD's actual state edges; the upmpdcli hook carries no session information and is used only to wake up observation, never to preempt or stop directly.

### 3.2 Session isolation

Every playback action produces a session. The receiving thread registers the latest session before enqueueing it, so events of an old session (a late stop, an old format) cannot change a new session. A PipeWire node ID is bound to its `object.serial`, and the Pulse index is resolved separately. Each granted lane must have exactly one active receiving stream; multiple streams on one lane, an unknown format, or a serial mismatch all fail and mute.

### 3.3 Pausing the previous sender

After the new input is committed successfully, HiFiMate independently asks the previous input to pause and reads the result back:

| Input | How it is paused | Readback |
|---|---|---|
| DLNA, Music Assistant | MPD `pause 1` | MPD `status` |
| Bluetooth | BlueZ `MediaPlayer1.Pause` | `Status` |
| AirPlay | DACP `pause` (credentials come from Shairport's `daid/acre/clip`; the service is looked up in mDNS by the sender's IP and DACP-ID) | DMAP playback status |

The result is recorded as `confirmed` (paused/stopped was observed), `accepted` (the request was accepted but not confirmed), `unsupported` (remote-control information or capability is missing), `error` (the request or discovery failed), or `stale` (the session had already ended, nothing was sent). The most recent 32 are in the `remote_pause` field of the IPC and are also written to the log, without the DACP token. A failed pause does not revoke the new playback right. DACP credentials are frozen at `pbeg` and reused by `pres` of the same connection; HiFiMate does not guess when the credentials are incomplete or mDNS cannot match uniquely. `playpause` is never used (it could turn a pause into a play), and HTTP redirects are not followed. An MPD pause only proves that the renderer paused; it does not prove that the control app's UI is in sync.

### 3.4 Switching from the control page

The control page's "Switch input" (「切换输入」) re-checks, inside the arbitration lock, whether the target lane has an uncorked stream (for Bluetooth it checks for a running BlueZ source). It uses the device/session/remote-control information most recently saved by the receiving thread, explicitly re-authorizes this retired session, and then goes through the normal candidate verification and commit flow. An input that has no data, or for which a playback request was never observed, cannot be switched to, and no attempt is made to make the sender resume.

## 4. Sample Rate Following (Format Transaction)

### 4.1 Steps

1. A playback edge produces a candidate; the current owner is not changed immediately.
2. Read the `Format` that PipeWire has negotiated and cross-check it against the Pulse receiving stream's sample spec and the target lane.
3. From the output device's capabilities, take the discrete sample rates for 2-channel S32_LE, generate the DSP configuration, and `ValidateConfigJson` it. The old signal path is not touched yet.
4. Mute the handoff fader (Aux4), stop the DSP, and read back that it is Inactive with an empty active configuration (a Stop return only means it was queued). Wait at most 3 seconds; failure is fatal.
5. Set the DSP's processing and capture sample rates and ALSA S32_LE, and turn off the resampler and rate adjust. When there is a FIR, require a filter for this sample rate.
6. Append a `_HiFiMateHandoff` Volume filter (2 channels, Aux4, maximum 0 dB) at the end of the configuration; once the mute readback passes, `SetConfigJson` (which starts processing by itself).
7. Verify: the RATE of the running node in `pw-top`, the DSP's configuration and Running state, the handoff fader, and the ALSA `hw_params`. The `params.Format.rate` of a virtual node may keep showing 48 kHz and cannot be used as clock evidence.
8. Check the source identity, the format, and the latest intent once more before committing the owner, `ready`, and `effective`.
9. Request the previous input to pause (3.3). Before releasing the handoff fader, verify the source, graph, DSP, DAC, session, and fader once more, then start the fade-in.

A format change of the same owner produces a new revision and goes through the flow again; a refresh with an already verified, identical format does not restart the DSP or fade in again. An old owner, old revision, or old serial can never configure the output.

### 4.2 Per-group forced sample rate

The global `clock.force-rate` is not used. `hifimate-camilladsp.service` sets `PULSE_PROP=node.force-rate=0`, and each time the DSP's capture stream starts, it forces only its own graph group (the granted lane and the DSP) to `capture_samplerate`; the other lanes keep their own sample rates. A global force would change idle lanes along with it. For example, when a phone's A2DP node was started inside a 96 kHz group and then switched to 48 kHz, PipeWire 1.4 could not recover (continuous XRuns, all-zero output).

### 4.3 Fade-in

The fade-in uses CamillaDSP's native Volume filter, interpolating per sample: while muted the gain is exactly zero, and on release it ramps from -100 dB to 0 dB, 400 ms by default (`fade_ms`, 20–2000). The master volume and mute are always under the user's control and the fade does not touch them. A stop or rebuild cancels a ramp in progress; there is no timer that unmutes after the fact. The state `fade` is `muted`, `ramping`, or `released`; `released` only means the configured duration has elapsed and is not a loudness measurement. Aux4 and `_HiFiMateHandoff` are reserved; a custom DSP cannot use or remove them.

### 4.4 Failure handling

On failure, `ready`, `effective`, `current`, and `binding` are cleared and the output is stopped. The last committed owner and the candidate waiting to be prepared are kept for diagnosis, and there is no automatic recovery with the old configuration. A failure with the same source identity and format is not reconfigured repeatedly; only new format evidence or a new playback action triggers a retry. When the hardware recovers while the input has not changed, playback must be started again. The wait for the receiving stream defaults to 5 seconds, the format readback retry window to 3 seconds, and external commands and RPCs to at most 5 seconds and 3 seconds respectively.

### 4.5 On bit depth and bit-perfect

The DSP works internally in floating point. 24-bit PCM is passed to the DAC in an S32_LE hardware container, and the container width does not represent the effective bit depth of the source file. HiFiMate follows the receiver's **decoded PCM**: when AirPlay (44.1 kHz/16 bit), Bluetooth, or the sender has already downsampled, the format of the original file cannot be recovered. Output that passes through the DSP (even with only Headroom) is not claimed to be bit-perfect.

### 4.6 Composition of the DSP configuration

Every DSP configuration (for all sample rates) is composed in this order: the preset Headroom (`config/host-stack/camilladsp.yml`, updated with the install) → personal filters (`config/dsp-filters.yml`, names prefixed with `_HiFiMateUser_`) → room correction (`config/room-correction.json`) → the handoff fader. After a Stop clears the active configuration, the next preparation prefers CamillaDSP's `GetPreviousConfig` and reads the base YAML only when that is unavailable; personal filters and room correction are put back from their files every time. Personal filters allow only `Filter` steps and the Biquad, BiquadCombo, Gain, Delay, DiffEq, and Conv types (Volume and Loudness would take over the fader). If the file has errors, playback continues as usual without the personal filters, and `hifimate-ctrl reload-filters` reports the reason; a reload during playback replaces the configuration directly without reopening the sound card.

## 5. Idle, Events, and Recovery

### 5.1 Releasing the DAC when idle

When the owner's stream is still there but silent (the sender paused without emitting a stop edge, sends only digital silence, or no longer writes data), PipeWire still fills the monitor with zeros. The generated DSP configuration defaults to `silence_threshold=-90 dB` and `silence_timeout=10 s` (the user's own non-zero settings are kept), so CamillaDSP enters `Paused`: it pauses processing and the ALSA PCM, but the device stays open, and validation therefore does not mute a session that is playing.

Only while there is an owner does arbitration read `GetState` every 5 seconds, without capturing PCM. Once `Paused` has lasted `idle_timeout_s` (default 300, 0 disables), it first asks the sender to pause, so that a later resume is guaranteed to produce a new playback edge. When the result is confirmed, accepted, or stale, it is handled as a stop: mute, Stop, close ALSA, retire the session. When the remote pause is unsupported or error, a resume would be unobservable, so the output is kept open and the same session is not retried.

### 5.2 Event-driven

PipeWire and Pulse subscriptions drive the checks, and repeated observations are merged; while idle, no PCM is captured and the DAC is not probed. A subscription EOF, a BlueZ bus disconnect, or a queue overflow makes the service exit and leaves it to systemd to restart. Unhandled exceptions and mute failures are both recorded as fatal and exit with a non-zero status, so a dead thread with the service still showing healthy cannot happen. At startup the service holds the single-instance lock and cleans up leftover sockets, reuses its own FIFO inode and drains old messages; the startup snapshot does not restore a previous playback right.

When MPD is unavailable (startup, restart, crash loop), the service reconnects with a 0.25–5 second backoff, reports once in the log after 15 seconds, and reports once more on recovery. When a connection is closed, that input's session is ended, and the state after reconnecting is treated as an initial snapshot. Command timeouts and protocol errors are still fatal.

### 5.3 Service dependencies

The DLNA and AirPlay adapters use only `Wants=` plus `After=` on `hifimate.service` and `hifimate-mpd.service`. With a hard dependency, a failed dependency start makes systemd end the start job with a `dependency` result, and such a failure is not retried by `Restart=`. AirPlay additionally uses `PartOf=hifimate.service` to follow explicit stop/restart, so that it reopens the rebuilt metadata FIFO. The two MPD instances use `Type=notify` and count as started only after they listen on their port. upmpdcli 1.9.17 loses a SIGTERM that arrives before startup completes, so `hifimate-dlna` has a 10-second stop timeout.

### 5.4 The AirPlay network interface

By default Shairport advertises on all network interfaces (including docker0, Compose bridges, and lo). MA uses host networking and sees all the advertisements, so it may pick a bridge address, which breaks clock synchronization and leaves the stream silent. Therefore, before every start, `airplay_net.py render` writes `interface` into the runtime copy of the configuration according to the default route (IPv4 first, lowest metric); an explicitly configured value is kept, and with no default route it stays automatic. `hifimate-airplay-net.service` watches `ip monitor route` and restarts Shairport when the default route's interface changes (merged over 2 seconds), and re-checks every 60 seconds.

### 5.5 cork

When the current owner's own stream is merely corked (same serial, still in the granted lane), the verified signal path is kept. After the graph clock change caused by a handoff, AirPlay flushes and resynchronizes, and corks briefly in the meantime; tearing down and rebuilding the signal path for that would stall the graph again and form a loop. While corked with no PCM the output is muted, a long silence is handled by the idle release, and a stream that really disappears is still handled as a failure.

## 6. Output Devices

`output_devices.py` identifies usable outputs from `/proc/asound` without opening the device:

- **USB**: reads the 2-channel S32_LE alternate settings from all stream files, without mixing in DSD and mono capabilities. The proc PCM info usually has no endpoint information, so a one-to-one mapping is possible only when there is exactly one playback stream and exactly one playback PCM; when the correspondence cannot be proven it is rejected, and HiFiMate does not guess by number.
- **Onboard analog output**: matches each playback PCM's info to the non-Digital 2-channel Audio Output of the same name and device number in the codec, and requires explicit Analog evidence; HDMI, DisplayPort, and S/PDIF are excluded. The related Speaker control converters take part in the capability intersection. Inheritance is not inferred when an independent PCM capability is missing.

At runtime only 44.1–768 kHz is handled, 2-channel S32_LE is required, and there is no resampling; duplicate card IDs and consecutive or invalid sample rates are rejected. `auto` prefers a single compatible USB device, uses the single onboard output only when there is no USB, and never falls back to the onboard output when there are several USB devices. When a device that was explicitly selected fails during operation, no other hardware is substituted.

Switching the output is a transaction. First a private `.hifimate-output-transaction.json` is written (the original and target contents with checksums); then the configuration is atomically replaced, only the hash of this one entry and the manifest signature are updated, and a directory lock serializes the whole thing. An active arbitration service is restarted only if its link belongs to this install; on failure the configuration and manifest are rolled back and the old configuration is restarted. After an interrupted process, recover with `hifimate-output --recover --apply`; install and uninstall require recovery first when they encounter an unfinished transaction.

## 7. Music Assistant Integration

### 7.1 Playback path

MA's official MPD Players provider connects to `127.0.0.1:6601`; MPD downloads the HTTP audio stream published by MA (8097) and outputs it to `music_assistant_dlna_in` (the name is kept from an earlier design and now has nothing to do with DLNA). MA and nginx use host networking, so loopback means the host. There is no dedicated renderer, SSDP, or network namespace.

The MPD provider forces Flow mode: the whole queue is one continuous stream. When Flow's sample rate is set to bit perfect, changing tracks across sample rates restarts the stream and produces one stop/play; HiFiMate handles it as a new session, and a late old stop is protected by session isolation. MPD state cannot tell such an automatic restart from the user pressing play, so after another input has taken over, if MA happens to restart its stream, it may preempt again; no speculative delay-based masking has been added.

### 7.2 Image and loader

The default image `ghcr.io/neqq3/ma-custom-loader-amd64:2.10.9` is a thin wrapper around the official `ghcr.io/music-assistant/server:2.10.9`: before startup it copies the providers in `MUSIC_ASSISTANT_CUSTOM_PROVIDERS_DIR` into MA's provider directory, and then runs the official entrypoint with the installing user's UID/GID, without modifying MA's code. On every run the installer copies the bundled Bilibili and Migu (咪咕) providers (`music-assistant/providers/`) into this directory.

`ma-provider-subscriber` downloads providers from GitHub into the same directory according to `sources` in `provider-subscriber/options.json`, which is empty by default. It uses the same UID/GID as MA. These directories are runtime data and are not in the install manifest; uninstall removes them only if they are empty.

`migu-api` ([migu-api-enhanced](https://github.com/Domdkw/miguMusic-api-enhanced)) has no published upstream image, and Compose builds it locally from a pinned commit, so the installer uses `pull --ignore-buildable`. After login it sees the Migu token in request URLs and writes it into the container log, so it listens only on loopback. All containers bind the host's `/etc/localtime`, and `MUSIC_ASSISTANT_TZ` is read by the installer from the system time zone.

### 7.3 What the installer does to MA

- **First user**: when MA has no users, the administrator is created through MA's own first-time setup endpoint (`POST /setup`) (`scripts/ma_setup.py`). The administrator must exist first, because MA treats the first user as the owner.
- **The `hifimate-ctrl` user and token**: the MA server is stopped briefly, and inside the MA image, using MA's own database and JWT code (`hifimate_ctrl_token.py`), the user `hifimate-ctrl` is created with the role "User" (「用户」; it can only control players and queues), and a ten-year token is issued and written to `config/ma-token` (0600, never printed). This user has no password and cannot log in to the web UI; a password set for it in the MA UI is cleared on reinstall. A reinstall keeps the token if more than a year remains; issuing a new token invalidates the old one.
- **MPD player** (`configure_ma_players.py`, writes `settings.json` only while MA is stopped): adds an MPD Players provider if there is none, with the manual address `127.0.0.1:6601`; fills in the missing settings for `mpd_127.0.0.1_6601`: WAV, the sample rates shared by the DAC and MA (16 and 24 bit), Flow sample rate bit perfect, and Native volume. Existing values are not changed; a provider that was added and then deleted by the user is not added again (recorded in `.hifimate-ma-players.json` in the data directory). After MA starts it creates the player from these settings, identical to one added by the user in the UI (verified on 2.10.9).
- **Other player settings** (same script): MA merges the HiFi DLNA of the same host into the generic `<name> AirPlay` player and makes its volume, and even fallback playback, go through the DLNA path, so HiFiMate's own DLNA protocol inside it is disabled (visible and restorable in the MA UI); the volume normalization of `<name> AirPlay` and of the MPD player queue is set to Disabled (only when it is still global or unset). On first install it waits up to 120 seconds for MA to discover `<name> AirPlay`.
- **MilkDrop visualization plugin**: created and enabled only if missing; it is not re-enabled after the user disables it.
- Before a reinstall, the deployment configuration (`compose.yml`, `.env`, the nginx configuration) and the image manifest are backed up to `.hifimate-deployment-backups/` in the data directory. The installer does not write MA's database (apart from the `settings.json` initialization above), and the user's `.env` is kept on reinstall.

### 7.4 nginx

The `web` container reverse-proxies MA (8095) on port 80, forwards `/ctrl/` to 5009 and `/_hifimate/` to 5008, and preserves WebSocket. Port 443 serves the same content with the local certificate, so the room correction page can use the microphone; `/roomfit` redirects to `/ctrl/roomfit/`, and the old `/spectrum/` redirects to `/ctrl/#spectrum`. nginx injects the spectrum floating window and `spectrum-tap.js` ahead of MA's own scripts.

## 8. Spectrum

The spectrum shows the sound the DAC is playing right now: it passively captures the DSP's input, multiplies it by the whole-chain response measured offline by CamillaDSP and by the live fader, and aligns it to the playback buffer. No patching is involved, and nothing is captured when nobody is watching. For the data sources, analysis method, resource bounds, and display details, see [SPECTRUM.md](SPECTRUM.en.md).

## 9. Control Page

### 9.1 Service

`ctrl.py` is based on the standard library's ThreadingHTTPServer, listens on 127.0.0.1:5009, and serves the `/ctrl/` page, state snapshots, logs, and SSE. All subscribers share one collector that runs about once a second, and polling stops after the last subscriber leaves. Read-only data comes from systemctl, pactl, pw-dump, the arbitration IPC, CamillaDSP's read-only RPC, and output identification, all with timeouts; an unknown metric is shown as unknown, and a service being active is never equated with the DAC producing sound. The spectrum reuses the one analysis at 5008.

Service operations use a fixed argv allowlist and do not go through a shell; the control page itself and PipeWire/WirePlumber may only be restarted. MA's five containers are managed as one group. Operations run asynchronously and report accepted/completed/failed, and a page reconnect does not replay a POST. Bluetooth pairing runs as a temporary user service, and SIGINT lets it turn off discoverability in its `finally`.

### 9.2 Security

- HiFiMate stores no control password: it is the Linux password of the run user (`hifimate` in a system install), and `ctrl_auth.py` verifies it by calling libpam through ctypes (service `hifimate`; `/etc/pam.d/hifimate` contains only pam_unix). The control service runs as this user, and pam_unix, through the setgid `unix_chkpwd`, lets an unprivileged process verify its own user's password but not another user's (verified on Debian 13, Fedora 43, and Arch), and it does not need to read `/etc/shadow`. For this reason `hifimate-ctrl.service` does not set `NoNewPrivileges` (it would strip the setgid of `unix_chkpwd`), nor `PrivateTmp` or `ProtectSystem` (in a user service these options put the service in a separate user namespace that has no shadow group); otherwise every verification fails. The service itself is an ordinary user with no sudo rights. Only `pam_authenticate` is called: pam_unix's account check needs root (without root it logs a setuid failure every time) and is meaningless for a nologin service account, and a locked password already fails at the authentication step. On a failed verification pam_unix adds its own delay of about 2 seconds; verification runs outside the lock and does not block other requests. The `hifimate` account's shell is nologin, and SSH is denied through `DenyUsers` in `/etc/ssh/sshd_config.d/hifimate.conf`, so this password is used only for the control page and the Samba share (`smbpasswd` is set to the same one).
- The service keeps only hashes of random login tokens, in memory, valid for 30 days; after `hifimate password` changes the password, the control service is restarted and all sessions become invalid.
- The cookie is HttpOnly and SameSite=Strict, scoped to `/ctrl/`; a POST validates its JSON and the same-origin Origin/Host; logins are rate-limited (5 per address, 20 globally).
- While the account has no password yet (locked), no password verifies; without libpam, login and all write operations are refused.
- HTTP on the LAN is not encrypted; across untrusted networks use an HTTPS reverse proxy or an SSH tunnel. Logs are readable from the LAN, so HiFiMate never writes any credential into the logs.
- The service runs as the installing user and can access that user's systemd, Docker, and BlueZ; it is not isolation against a malicious local user.

### 9.3 Now playing

| Input | Track information | Controls |
|---|---|---|
| DLNA | MPD tags | MPD: play/pause, previous/next, seek |
| Music Assistant | MA's queue API (with the `hifimate-ctrl` token) | MA queue: play/pause, previous/next, seek |
| AirPlay | Shairport metadata | DACP: play/pause, previous/next, no seeking |
| Bluetooth | BlueZ (AVRCP) | BlueZ: play/pause, previous/next, no seeking |

MA sends the whole queue to MPD as one Flow stream, and MPD only knows "Music Assistant", so MA's track, progress, next track, and seeking all come from MA's queue API. Without a token only "Music Assistant" is shown, and play/pause still works.

When the floating window on the MA page has "HiFiMate output" (「HiFiMate 输出」) selected, it uses the control page's API (operations work only if the same browser has logged in to the control page); when "This browser" (「本浏览器」) is selected, it reads from and controls MA using the MA page's own login information (`ma_access_token`) and the web player id (`sendspin_webplayer_id`).

### 9.4 Lyrics

`lyrics.py` looks up only the track currently playing, and caches results per track:

1. For a track from MA's Migu (咪咕) provider, the Migu lyrics are fetched directly with the song id.
2. Other tracks are matched on NetEase Cloud Music (网易云音乐): first `/search/match` (with the md5 persistId), then `/cloudsearch`; the title must match, the artist must match, and the duration may differ by at most 4 seconds. When word-by-word lyrics are available (the yrc of `/lyric/new`), they are highlighted word by word.
3. If NetEase has nothing, search on Migu (咪咕).
4. The "artist" of a Bilibili video is usually the uploader, and the title carries decorations (such as "周杰伦 - 晴天MV 2160P修复版"). The title is cleaned first, and the two parts are split out of "artist - title" (both orders are tried). The artist is not required to match, but the title must match and the duration may differ by at most 2.5 seconds, and other versions such as "(原唱 …)" (original singer) or "(钢琴版)" (piano version) are not accepted. When the duration does not match, showing nothing is preferred.

## 9.5 EQ Page and Settings Page

- **EQ** (`hifimate/eq.py`, `gui/eq.*`): the file `config/dsp-filters.yml` remains the only source of truth, and the page edits a draft. A read returns a fingerprint of the file contents, and a save is rejected if the fingerprint does not match (the file was changed on the host). Before saving, the old contents are stored as a version (`state/eq/versions/`, the latest 10); after the atomic replacement the arbitration service is made to reload (without reopening the sound card). Curves: the draft, the room correction, and the two combined are each handed to an offline CamillaDSP (`dsp_model.py`, 48 kHz, 32768 points) to measure; the magnitude of H[c][c] is taken and interpolated over logarithmic frequency into 240 points. The same configuration is cached, and one measurement takes about 0.1 seconds. When the combined maximum boost plus the preset Headroom exceeds 0 dB, the page warns of possible clipping. Import parses the REW, AutoEQ, and Equalizer APO `Filter N: ON PK Fc … Gain … Q …` format (including `Preamp`, `Channel`, and `BW Oct`). The page treats each (pipeline step, filter) as one row, and on save writes one step per row; a filter's on/off switch is the step's `bypassed`.
- **A/B bypass** (「旁路对比」): an in-memory switch of the arbitration service. When on, it adds `bypassed` to all steps of the personal filters, and the file is unchanged; restarting the service or turning the switch off restores them.
- **Settings** (`hifimate/settings.py`, `gui/settings.*`): playback settings are written to `config/settings.json` (user data, not in the install manifest); the arbitration service layers it on top of `hifimate.conf` at startup, and the sample rate check when switching the output also reads it. Music source subscriptions directly change the subscriber's `options.json` (other keys are kept), and then run as one asynchronous operation: restart the subscriber, wait for this round's "Next check in" to appear in its log, record errors and warnings in events, and then restart the MA server.

## 10. Room Correction

### 10.1 Measurement

The measurement page (`/ctrl/roomfit/`, HTTPS) turns off echo cancellation, noise suppression, and automatic gain on the iPhone and uploads the recording losslessly (about 3 MB each). The sweep is played by the DLNA MPD through the real signal path (arbitration → CamillaDSP → DAC), which interrupts the music currently playing. The host deconvolves to get the impulse response, windows it to get the frequency response, and then subtracts the selected phone's microphone curve (compensation of at most 12 dB; low-frequency bands where the microphone needs more than 6 dB of compensation are marked as untrustworthy, for example below about 30 Hz on the iPhone 15 Pro Max).

The wizard flow:

1. Sweep once at the center position, choose the sweep level by the measured signal-to-noise ratio (target 38 dB, recording peak no higher than -6 dBFS), and prompt to turn the volume up when it is insufficient.
2. The center plus left, right, front, and back about 30 cm away, 5 points in total, at least 3. At each point the left and right channels are swept, and the result is judged good / acceptable / needs re-recording, with the reason explained (clipping, signal-to-noise ratio, iOS audio processing, too large a difference from the center).
3. The 1/6-octave frequency responses of the points are averaged in dB (the center weighted 2), after first subtracting any room correction that is already enabled; a peak-cutting-only PEQ is fitted over 35–350 Hz (target +3 dB below 60 Hz, 1/3-octave fitting, Q ≤ 5, at most 8 per channel), and the predicted response before and after correction is displayed.
4. On apply, it is written to `config/room-correction.json` and takes effect immediately during playback without reopening the sound card. The most recent 3 profiles are kept in `state/roomfit/profiles/`.

### 10.2 Microphone curves

Each file in `hifimate/mics/*.json` is the curve of one measured sample unit, and states the link to the original source and the measurement method: Faber Acoustical, in an anechoic chamber, placed a laboratory reference microphone (PCB 378B02 1/2″ for the 15 Pro Max through the 17 Pro, PCB 377C01 1/4″ for the 18 Pro) close to the iPhone's bottom microphone and measured with SignalScope's dual-channel FFT and iOS measurement mode, with pressure-response correction applied from the reference microphone's factory data (the blue curve in the figure). HiFiMate reads the values from the published figure on a coordinate grid, taking the median in each 1/24 octave, with an error of about ±0.3 dB; points that are cut off against the bottom edge of the figure are not included. The `models` of each file lists the same-generation models that reuse this curve.

### 10.3 Certificates

The iPhone allows only HTTPS pages to use the microphone. At install time `tls.py` generates a local CA (`config/tls/`, kept on reinstall) and a server certificate (the LAN IP and `<hostname>.local`, valid for 397 days); when the address changes or expiry approaches, the installer re-signs it, and the CA stays the same.

## 11. Bluetooth

- WirePlumber registers only the A2DP Sink (receives media audio, with no HFP/HSP calling) and turns off seat monitoring: when running headless, the user session is not on the active seat, and with seat monitoring on, the BlueZ monitor would never start and would therefore never publish an Audio Sink.
- The Bluetooth input node is routed to `bluetooth_in`, and the volume is controlled only by the phone (AVRCP absolute volume); WirePlumber neither restores nor saves it.
- `hifimate-bluetooth.service` sets the name on all adapters through BlueZ `Adapter1.Alias` (including ones plugged in later), and records the original name by MAC address under the run user's `~/.local/state/hifimate/`; uninstall restores only names that still belong to HiFiMate and keeps names the administrator changed later. It also confirms every 10 seconds that the adapter is not discoverable outside the pairing window (after a bluetoothd restart the default is pairable).
- During the pairing window an agent is registered that confirms pairing automatically; the first device that pairs successfully is marked trusted, and the window closes right after.
- `--setup-bluetooth-speaker` uses sudo to write a marked device class (Class 0x000414, Audio/Video · Loudspeaker) and `JustWorksRepairing = confirm` into `/etc/bluetooth/main.conf` (this lets a previously paired phone re-pair through the pairing window's agent; BlueZ's default of never rejects it, and the phone shows a wrong-PIN error), and then restarts bluetoothd. It refuses if the administrator has already set a Class. The ownership record is in `/var/lib/hifimate-bluetooth-speaker/<UID>.json`, and uninstall removes only the managed lines that are unchanged.
- BlueZ binds sessions through the pairing of device and MediaPlayer path; a pause or removal of an old player of the same device does not affect the new player.

## 12. Volume

The volume of all four inputs is adjusted only by their own controllers (the DLNA controller, MA, the AirPlay sender, the phone). WirePlumber neither restores nor saves the volume of these streams (`state.restore-props = false`), and a new stream always starts at 0 dB; otherwise the two MPD instances would share the `media.role=Music` record, and the volume of one would leak into the other (for example 54%, which is -16 dB). When MA's Volume Control is set to Native, what MA adjusts is the PipeWire stream volume of MPD 6601. CamillaDSP's master volume is independent of these.

## 13. Realtime Scheduling

- `realtime.py` is a small wrapper run before exec, used only for CamillaDSP, the two MPD instances, and Shairport. With `auto` (the default) it first reads `/sys/kernel/realtime`, then `CONFIG_PREEMPT_RT` in `/proc/config.gz` or `/boot/config-<release>`, and requests realtime only after confirming a realtime kernel; `on` also requests it on a normal kernel; `off` does not request it.
- When requesting, it first limits RLIMIT_RTTIME to at most 5 seconds (measured: MPD's main thread takes about 220 ms to start and Shairport's startup thread about 1.5 seconds without blocking, and 200 ms would get them SIGKILLed), and then requests SCHED_RR priority 5. With insufficient permission it writes a log entry and continues with normal scheduling. The kernel's `sched_rt_runtime_us` throttle is not changed.
- PipeWire's own module-rt requests level 83/88 by default and falls back to RTKit when that exceeds RLIMIT_RTPRIO 5; a headless user is not in an active session, so polkit refuses RTKit, and the audio thread silently stays on normal scheduling. Therefore the managed `52-hifimate-realtime.conf` snippet sets `rt.prio` to 5, does not touch nice (with RLIMIT_NICE at 0 a failing nice also turns realtime off), and turns off RTKit and the portal; WirePlumber ignores `module.rt.args`, so its snippet appends a module-rt with the same arguments. Verification: `data-loop.0` shows `FF 5` in `ps -L -o cls,rtprio,comm -p <pid>`.
- `--setup-realtime` uses sudo to create only `/etc/systemd/system/user@<UID>.service.d/90-hifimate-rtprio.conf`, raising the soft and hard RTPRIO limits to at least 5 and keeping any higher value; it does not change PAM and does not restart the user manager (so it takes effect after logging in again). The ownership and the file's SHA256 are recorded in `/var/lib/hifimate-realtime/<UID>.json`, bound to the UID and the install directory; when the user's configuration explicitly sets `DefaultLimitRTPRIO` lower, the conflict is reported before sudo is called.

## 14. Installer

- **System install and user install**: `sudo hifimate install` (which is also what the one-line installer script runs) runs the steps in `hifimate/system.py` as root: install the software packages; create the user `hifimate` (an ordinary UID rather than `useradd --system`, nologin, home directory `/var/lib/hifimate`, added to the audio, bluetooth, and docker groups); prepare `/opt/hifimate` and `/srv/hifimate/music` (setgid 2775); make the two root settings with `realtime_permissions.py` and `bluetooth_speaker.py` using that user's UID; `loginctl enable-linger` and start `user@<UID>.service` (restarting it when a group was newly added or a new RTPRIO limit applies, otherwise the services would not get them); then run `scripts/install.sh` with `runuser -u hifimate` plus that user's `XDG_RUNTIME_DIR` and session bus; and finally write the Samba share (a marked section at the end of `smb.conf`, `smbpasswd`, the Avahi `_smb._tcp` service file, the firewalld opening). The password is read by root (the file may be readable only by root) and passed to `install.sh` through an environment variable. The install options are recorded in `/etc/hifimate/install.json`. Running `hifimate install` without root is a user install: it installs into the current user's `~/hifimate`, creates no user, shares nothing, and system settings need explicit options and sudo. When a system install exists, an ordinary user running `hifimate` is prompted to use sudo instead.
- **Why an ordinary UID**: journald gives separate journal files only to users outside the system UID range. With its own journal file, `hifimate` lets the control page read its own services' logs without any extra permission; with a system user it would have to join the systemd-journal group, which could read the whole machine's logs, while the control page's logs are visible to the LAN. When reading logs it still explicitly restricts to its own `_UID=`.
- **Two entry points, one install path**: the `hifimate` command (the pipx package) restores `hifimate/_dist/` and the `hifimate` package from the wheel into a source tree in a temporary directory, and then runs the same `scripts/install.sh` and `scripts/uninstall.sh` as a source checkout. The installer uses only the standard library; runtime dependencies come from the distribution's packages (`scripts/host_stack.py` chooses apt, dnf, pacman, or zypper according to `/etc/os-release`, and the package list is in `config/host-stack-manifest.json`).
- **CamillaDSP**: `scripts/camilladsp_release.py` downloads the official 4.1.3 PulseAudio build, checks the pinned SHA256, `--version`, and `pulse-backend` in `--help`, and installs it to `bin/hifimate-camilladsp`; the origin is recorded in `share/hifimate/camilladsp/release.json`, and the archive is cached in `~/.cache/hifimate`. For offline installs use `--camilladsp-archive` (add `--camilladsp-sha256` for a non-pinned archive).
- **Ownership manifests**: `.hifimate-install-manifest` in the install directory records the hash of every file the install owns, and `.hifimate-user-config-manifest` records the PipeWire/WirePlumber snippets and unit links published to the run user's `~/.config`; both have signature files. Before a reinstall or uninstall, all hashes are verified first, and it is checked that the targets and their parent directories contain no symlinks or unowned files; an owned file that was modified by hand is refused for overwrite or deletion. User data (the MA token, personal filters, room correction, certificates, measurement records, MA's provider directory) is outside the manifests and is kept on reinstall. When an install fails midway, `.hifimate-install-in-progress` and the error message are kept so that it can be retried.
- **Atomic replacement**: when upgrading owned snippets, all new files, the manifest, and the signature are staged first, then replaced file by file atomically, and rolled back on failure.
- **systemd units**: at install time, placeholders such as `@PREFIX@` are rendered into absolute paths, and the host programs (mpd, upmpdcli, shairport-sync) are resolved from PATH to their actual paths; when not found, the command name is kept and systemd's own search path is used. The `base_config` in `config/hifimate.conf` points to the current install directory on every install, so room correction and personal filters can still be found after the install directory is moved.
- **Root operations**: only `--install-deps`, `--setup-realtime`, and `--setup-bluetooth-speaker` use sudo; the ownership records of the latter two are root-owned files, and uninstall restores according to the records, not relying on user-writable markers.
- **Unattended**: the password is read only from an environment variable or a file (`scripts/install_secrets.py`) and is validated before any change.

## 15. Verification Status and Known Limitations

Verified on real hardware (Debian 13, a USB DAC that supports up to 768 kHz, MA 2.10.9):

- A 192 kHz/24 bit file played through MA: PipeWire, the DSP, and the DAC are all at 192 kHz, the ALSA readback is S32_LE, and `resampled=false`; track changes in a Flow queue between 192 and 44.1 kHz are followed to the DAC.
- Two-way takeover between DLNA and MA, and between AirPlay and MA, with the previous input paused; a late old stop does not revoke the new owner, and after a stop the previous input is not resumed.
- In a temporary PipeWire/WirePlumber graph, negotiating 44.1 kHz → 768 kHz → 44.1 kHz/24 bit (`scripts/check-format-stack.py`, without opening the sound card); MPD 0.24's native PipeWire output passes as well.
- The spectrum is frame-for-frame identical to the method that reads the ALSA output with a patch (see [SPECTRUM.md](SPECTRUM.en.md)).

The automated tests cover all 12 switching directions among the four inputs, preparation failure, old sessions, remote pause unsupported, recovery, same-source format changes, and fade cancellation; the external commands, DSP RPC, and receivers are all fakes and cannot replace actual playback.

Not yet done, or limited:

- Real-hardware switching between Bluetooth and the other inputs, remote-pause compatibility across various senders, how the fade-in sounds, and whole-machine CPU measurement while idle.
- On distributions other than Debian 13, only dependency installation and checks were verified, with no real playback.
- Very short state changes in MPD idle and BlueZ may be merged before the readback; a play/resume that cannot be observed is not guessed from continuing PCM, and the user can pause and then play.
- AirPlay metadata without a connection-number payload distinguishes connections only to a limited degree.
- Switching on a shared DAC has a silent gap and is not seamless.
- A sample rate counts as supported only after it has been played on the actual device; the capabilities a DAC advertises, configuration validation, and isolated tests are not the same as passing actual playback.
