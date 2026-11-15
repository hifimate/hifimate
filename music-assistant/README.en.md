[中文](README.md) | **English**

# Music Assistant guide

[Music Assistant](https://www.music-assistant.io/) (MA below) manages a local music library and streaming music sources, and lets you play and queue from a web page or a phone.

HiFiMate deploys the [Music Assistant Custom Loader](https://github.com/neqq3/ma_custom_loader) maintained by [neqq3](https://github.com/neqq3)
(the official MA 2.10.9 image plus the ability to load custom music providers at start-up, without modifying MA itself), together with the same project's provider subscriber. Thanks to the Music Assistant team and neqq3.

HiFiMate deploys it together by default, with these integrations:

- When playing through HiFiMate's player, the sound reaches the DAC at the original sample rate and takes part in automatic switching like every other input;
- Two bundled music providers, Bilibili and Migu (咪咕);
- A HiFiMate spectrum floating window at the bottom right of the MA web page, showing what is playing and the lyrics;
- The HiFiMate control page can show and control MA's play queue (tracks, progress, previous/next, dragging).

Web address: `http://<host IP>/`. Log in with the administrator account created during installation; once logged in, the browser does not need to log in again for 90 days.

## First-time setup

The installer has already done the player part for you: it adds HiFiMate to MA as a player (named `<name> MA`) and sets up lossless output, all the sample rates the DAC supports,
playback at the original sample rate, and volume control. All you need to do is tell MA where the music is:

### 1. Add local music

The music library on the host, `/srv/hifimate/music` (changeable with the install option `--music-directory`), is `/media` inside MA, read-only. For putting songs into it see [README](../README.en.md#adding-music-to-the-library).

1. Settings → Providers → Add, and choose **Filesystem (local disk)**.
2. Enter `/media` as the path and save. MA starts scanning the library.

### 2. Add streaming music providers (optional)

In the same Add list, add QQ Music, NetEase Cloud Music (网易云音乐), Spotify and so on, as well as HiFiMate's bundled **Bilibili** and **Migu Music**; see [Bundled music providers](#bundled-music-providers) below.

Then choose `<name> MA` in the player list to play.

### Why choose `<name> MA` for playback

MA also lists `<name> AirPlay` in the list. That is HiFiMate's AirPlay input, but AirPlay is limited to 44.1 kHz/16 bit.
`<name> MA` uses a dedicated connection internal to HiFiMate and can play at each track's original sample rate.

### Player settings made by the installer

You can see the values below in this player's settings. Settings you have changed yourself are not reset on reinstall;
if you delete this player's provider (MPD Players) in MA, reinstalling will not add it back either.

| Setting | Value | Reason |
|---|---|---|
| Output codec | WAV | MA's default, MP3, is lossy |
| Supported sample rates | All sample rates the DAC supports, 16 and 24 bit | MA defaults to only 44.1/48 kHz, 16 bit, so high-sample-rate tracks would be downsampled |
| Flow mode sample rate | bit perfect | Output at each track's original sample rate |
| Volume Control | Native | MA's volume directly controls this stream; 100% is the original volume |
| Queue's Volume normalization | Disabled | Do not let MA change the volume automatically |

It is also recommended to turn off DSP in the player settings and Crossfade in the queue settings, and to use HiFiMate's EQ and room correction instead.

If you change the DAC after installation, running `hifimate install` again will not change the existing sample-rate settings (they may be your own choices); tick them manually here when needed.
If you tick a sample rate the DAC does not support, playing such a track will be silent (HiFiMate does not resample behind your back), and the control page will show the reason.

The sample rates MA supports are 44.1, 48, 88.2, 96, 176.4, 192, 352.8 and 384 kHz. Each time this player changes sample rate, playback pauses briefly;
this is caused by MA re-establishing the audio stream.

## Playback

In the MA web page or the MA phone app, choose `<name> MA` to play. Once playing, both the HiFiMate control page and the spectrum floating window on the MA web page show the track, progress and lyrics.

While MA is playing, starting playback from a phone over AirPlay, DLNA or Bluetooth switches to the phone's sound and MA pauses automatically; the reverse works the same way.

## Bundled music providers

Both providers are optional, and are enabled only after you add them in Settings → Providers → Add. If you do not want them installed, add
`--without-ma-provider bilibili` and `--without-ma-provider migu` at install time, or use `--without-ma-providers` to leave out both.

- **Bilibili**: plays the audio tracks of Bilibili videos, with search, uploader submissions, multi-part videos, favourites folders and music charts. You can log in to your own account by scanning a QR code, so as to reach your favourites folders.
- **Migu Music (咪咕音乐)**: accesses the Migu catalog through the Migu API service on the host, with search, albums, artists, playlists, charts and lyrics. You can log in to your own account.

What can be played depends on the entitlements of your own account; see the [README disclaimer](../README.en.md#disclaimer).

## Lyrics

Lyrics are provided by the NetEase Cloud Music (网易云音乐) API service (`hifimate-ncm-api`) and the Migu API service (`hifimate-migu-api`) on the host; the corresponding music providers do not need to be added. For tracks from Migu, Migu's lyrics are used directly; for other tracks, the lyrics are first matched on NetEase Cloud Music by title, artist and duration, and Migu is queried if nothing is found.
When there is no match it prefers to show nothing, and the control page shows the reason. These two services listen only on the host's internal address and are not open to the LAN.

## Third-party music providers

The MA community has many more third-party music providers. HiFiMate deploys a subscriber (`hifimate-ma-provider-subscriber`) that can download and update them automatically from GitHub.
It subscribes to no repositories by default.

Third-party music providers are not part of HiFiMate; their code runs inside MA and can access all of MA's data. Review the code yourself before adding one, and make sure that using it complies with the law where you live and the platform's terms.

How to add one:

1. Open HiFiMate's settings page (the gear at the top right of the control page, `http://<host IP>/ctrl/settings/`) and log in with the control page password.
2. Under "Third-party provider subscriptions" (「第三方音乐源订阅」), enter a repository (`owner/repo`, or a GitHub, Gitee or GitCode repository URL), click "Add" (「添加」), then click "Save and sync" (「保存并同步」).
   The page has the subscriber download these repositories, and then restarts MA to load them, which takes about 1 minute. Errors during syncing are shown in the control page's events.
3. In MA's Settings → Providers → Add, add the new music provider.

The page also shows each repository's version, last sync time and which music providers it installed; when deleting a repository you can choose to delete the providers it installed as well.
After that, the subscriber checks for updates automatically every 6 hours. The settings are stored in `/opt/hifimate/music-assistant/provider-subscriber/options.json` and can also be edited by hand.

## Changing the web port

When port 80 on the host is taken by another web service:

1. Edit `/opt/hifimate/music-assistant/.env` (`sudo nano …`) and change `MUSIC_ASSISTANT_HTTP_PORT=80` to, for example, `8080`.
2. Apply the change:

   ```sh
   cd /opt/hifimate/music-assistant
   sudo docker compose up -d --wait
   ```

Afterwards MA is at `http://<host IP>:8080/` and the control page at `http://<host IP>:8080/ctrl/`. The HTTPS port used for room correction works the same way, as `MUSIC_ASSISTANT_HTTPS_PORT`.

`.env` belongs to you; neither reinstalling nor upgrading overwrites it.

## Data and backup

- MA's data (accounts, library index, settings) is in `/var/lib/hifimate/music-assistant/`.
- Before every reinstall, the installer backs up MA's deployment configuration (`compose.yml`, `.env`, web configuration) to
  `.hifimate-deployment-backups/` under the data directory. This is not a data backup.

To back up MA's data, stop it first, copy the whole data directory, then start it again:

```sh
cd /opt/hifimate/music-assistant
sudo docker compose stop server
sudo cp -a /var/lib/hifimate/music-assistant /root/ma-backup-$(date +%F)
sudo docker compose start server
```

## Upgrading MA

HiFiMate uses the verified MA 2.10.9. The image version is written in `.env`, and neither reinstalling nor `hifimate upgrade` changes it.
If you want to change the version yourself (you must confirm compatibility yourself):

1. Stop playback and back up the data as in the previous section.
2. Edit `MUSIC_ASSISTANT_IMAGE` in `/opt/hifimate/music-assistant/.env` (`sudo nano …`).
3. Pull and restart:

   ```sh
   cd /opt/hifimate/music-assistant
   sudo docker compose pull
   sudo docker compose up -d --wait
   sudo docker compose ps
   ```

To go back to the previous version, change `.env` back, restore the backed-up data directory, and run step 3 again.

## What each container is

| Container | Purpose |
|---|---|
| `hifimate-music-assistant` | MA itself (the official 2.10.9 image plus a start-up script that loads custom music providers) |
| `hifimate-music-assistant-web` | Web entry point (nginx): the MA web page, the control page, the spectrum and room correction |
| `hifimate-ncm-api` | NetEase Cloud Music (网易云音乐) API, used for lyrics |
| `hifimate-migu-api` | Migu Music API, used for the Migu provider and lyrics; built locally at install time |
| `hifimate-ma-provider-subscriber` | Third-party music provider subscriber |

`hifimate status` shows their state. For technical details see [ARCHITECTURE.en.md](../docs/ARCHITECTURE.en.md#7-music-assistant-integration).
