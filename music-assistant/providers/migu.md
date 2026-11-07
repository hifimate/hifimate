# Migu Music provider for Music Assistant

把咪咕音乐作为 Music Assistant（MA）的音乐源，做法和网易云一样：MA 不直接请求咪咕，而是请求本机的 [migu-api-enhanced](https://github.com/Domdkw/miguMusic-api-enhanced) 服务（相当于 `NeteaseCloudMusicApiEnhanced` 之于网易云）。

随 HiFi 提供，在 **MA 2.10.9**（HiFi 的默认镜像）上测试。

## 功能

| MA 概念 | 咪咕数据 | 说明 |
|---|---|---|
| 曲目 Track | 歌曲 | id 为 `contentId`；附带 LRC 歌词 |
| 专辑 Album | 专辑 / 数字专辑 | 普通专辑 id 为 `albumId`；数字专辑为 `d<contentId>` |
| 艺人 Artist | 歌手 | “热门曲目”为歌手歌曲第一页（50 首）；专辑含数字专辑 |
| 歌单 Playlist | 歌单 / 榜单 / 我的歌单 | 榜单 id `rank_<rankId>`；自建歌单 `my_<id>`；收藏歌单用公开 id |
| 推荐 | 咪咕榜单；登录后加“今日推荐” | |
| 搜索 | 歌曲、专辑、歌手、歌单 | |

### 登录与音质

| 方式 | 能播放的 | 音质 |
|---|---|---|
| 不登录 | 非会员歌曲 | 128kbps MP3 |
| 短信验证码登录（会员账号） | 会员歌曲；“我的歌单”“今日推荐” | 按选项：Hi-Res（24bit FLAC）→ 无损（FLAC）→ 320k → 128k |

- 选项 **Preferred audio quality** 是上限。歌曲或账号拿不到这一档时自动降一档，日志（debug）会记下实际音质。
- 部分歌曲需要**白金会员**（例如周杰伦）。普通会员播放这类歌曲时会报“版权方要求，该歌曲需开通白金会员后播放 (440013)”。
- 短信请求太频繁时，咪咕会要求图形验证码，设置流程没法显示它。这时在浏览器登录 music.migu.cn，复制 `pacmtoken` cookie，在设置里选“Paste a pacmtoken”。
- 服务会轮换 `pacmtoken`，provider 拿到新值就写回 MA 配置。token 失效时 MA 会提示重新登录。

## 目录结构

```
migu/                   provider 包（部署时整个目录放进 MA 的 custom_providers/）
  __init__.py           MiguProvider：MA 接口实现
  migu_client.py        唯一请求 migu-api-enhanced 的模块，返回普通 dict
  parsers.py            咪咕数据 → MA 模型
  setup_flow.py         设置流程：服务地址 + 短信登录 / 粘贴 token / 匿名
  constants.py, manifest.json, strings.json, icon*.svg
（HiFi 仓库里：本目录 music-assistant/providers/migu/，测试 tests/providers/migu/，
 冒烟测试 scripts/providers/migu_live_smoke.py）
  live_smoke.py         对真实服务跑一遍完整链路，并用 ffmpeg 解码音频
```

## 关键设计

**服务。** migu-api-enhanced 没有发布镜像。HiFi 的 Compose（`music-assistant/compose.yml` 的 `migu-api`）从固定 commit 构建，以安装用户 UID 运行，根文件系统只读，去掉全部 capabilities，只监听 `127.0.0.1:3004`。provider 默认地址为 `http://127.0.0.1:3004/migu`。路由前缀是 `/migu`，上游文档写的 `/api` 不对。

**为什么只监听本机。** 登录时手机号和验证码会经过这个服务，之后每个请求都在 URL 里带 `pacmtoken`，而服务会把请求 URL 打进日志。所以不要用公共实例，也不要把端口开到局域网。

**播放。** 调用 `/url/v2`（登录后带 `pacmtoken`），音质从选项上限开始，只尝试歌曲 `audioFormats` 里有的档位，最后总会试 PQ。实际音质以返回的 `audioFormatType` 为准，没有就从 CDN 路径判断。以 `StreamType.HTTP` 交给 MA 的 ffmpeg，不需要额外请求头。CDN 地址的有效期看不出来，按 10 分钟处理。

**解析。** 咪咕各接口的结构不统一：搜索把歌曲包在 `song` 里，歌手页包在 `songItem` 里，榜单把整首歌塞在 `songData` JSON 字符串里，里面的嵌套字段还是 Python repr。`iter_songs` / `iter_playlists` 递归查找“像歌曲 / 像歌单”的 dict，并按 id 去重，所以页面布局变化不会直接导致解析失败。登录后接口（我的歌单、今日推荐）的返回结构没有文档，也靠这种查找方式适配。

**图片。** 咪咕图床允许外站引用，图片设为 `remotely_accessible=True`，取最大的一张（`img3` / `imgSizeType` 最大，800×800）。

**登录保活。** 咪咕的登录随每次登录态请求顺延（每次都会返回新的 `pacmtoken`，provider 立刻保存），但闲置几个小时就会失效：
实测闲置 1.4 小时仍然有效，闲置 3.2 小时后被登出（错误「手机号和IMEI不能同时为空」/「uid不能为空」）。所以登录后 provider
每 40 分钟调用一次 `/user/info` 续期；咪咕真的登出时（例如在别处退出、改密码）只记一条警告，需要在 MA 里重新登录。

## 开发

测试需要 MA 本身，在 HiFi 使用的 MA 镜像里运行（会按 `manifest.json` 安装依赖）：

```bash
scripts/test_ma_providers.sh                      # 两个 provider，默认镜像
scripts/test_ma_providers.sh <image> migu       # 指定镜像和 provider
```

对真实服务的冒烟测试是 `scripts/providers/migu_live_smoke.py`，需要装有 MA 的 Python 环境运行
（登录后用 MIGU_PACMTOKEN=... 或 --token 测会员音质和我的歌单；--api http://127.0.0.1:3004/migu）。

## 部署

由 HiFi 安装器负责：`scripts/install.sh … --with-music-assistant` 默认把它复制到
`MUSIC_ASSISTANT_CUSTOM_PROVIDERS_DIR`，MA 启动时加载（`--without-ma-provider migu` 跳过）。
然后在 MA 的「设置 → 音乐提供商 → 添加」里选 Migu Music（服务 `migu-api` 也由 HiFi 的 Compose 部署）。

## 已知限制 / 待办

- 登录后的接口（我的歌单、收藏歌单、今日推荐、播放 SQ/ZQ24）还没有用真实会员账号验证过；匿名部分已对真实接口验证。
- “我喜欢”按自建歌单处理。曲库同步只导入歌单，不支持在 MA 里收藏或取消收藏。
- 未实现：歌词翻译（trc）、逐字歌词（mrc）、MV、相似歌曲、电台。
- Hi-Res 的采样率在列表里按 48kHz 显示（咪咕不提供），播放时由 ffmpeg 读取真实值。
- `manifest.json` 的 `codeowners` 还是空的。
