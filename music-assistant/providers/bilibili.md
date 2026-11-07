# Bilibili provider for Music Assistant

把 B 站视频的音轨作为 Music Assistant（MA）的音乐源：搜索、UP 主投稿、多 P 视频、收藏夹、音乐中心榜单。

随 HiFi 提供，在 **MA 2.10.9**（HiFi 的默认镜像）上测试。

## 功能

| MA 概念 | B 站数据 | 说明 |
|---|---|---|
| 曲目 Track | 视频（单 P）/ 视频的某一 P | id：`BVxxxxxxxxxx`（P1）或 `BVxxxxxxxxxx_p3` |
| 专辑 Album | 多 P 视频 | 每一 P 是一首曲目 |
| 艺人 Artist | UP 主 | id 为 mid；“热门曲目”= 最新投稿 50 条 |
| 歌单 Playlist | 收藏夹 | id：`fav_<media_id>`；登录后同步到 MA 库 |
| 推荐 | 音乐中心「热歌榜」「新歌速递」 | 无需登录 |
| 搜索 | 视频（默认仅音乐区）+ UP 主 | |

登录（扫码）可选：

- **不登录**：只能访问公开内容，音质最高 192kbps AAC。
- **扫码登录**：可同步自己的收藏夹；大会员账号在有 Hi-Res 音轨时可播放 FLAC。

## 选项

- **Preferred audio quality**：`lossless`（有 FLAC 就用，否则用最好的 AAC）/ `high`（最好的 AAC）/ `standard`（132kbps）
- **Search the music zone only**：搜索限定音乐区（tid=3），默认开启
- **Clean up video titles**：清理标题装饰（`【Hi-Res无损】｜《晴天》- 周杰伦` → `晴天`），默认开启

## 目录结构

```
bilibili/               provider 包（部署时整个目录放进 MA 的 providers/）
  __init__.py           BilibiliProvider：MA 接口实现
  bili_client.py        适配层：唯一接触 SDK 和 HTTP 的模块
  parsers.py            B 站数据（dict）→ MA 模型
  setup_flow.py         设置流程：扫码登录 / 匿名
  constants.py, manifest.json, strings.json, icon*.svg
（HiFi 仓库里：本目录 music-assistant/providers/bilibili/，测试 tests/providers/bilibili/，
 冒烟测试 scripts/providers/bilibili_live_smoke.py）
  live_smoke.py         对真实 B 站接口跑一遍完整链路，并用 ffmpeg 解码音频
```

## 关键设计

**适配层隔离 SDK。** 只有 `bili_client.py` 依赖 [bpi-py](https://github.com/Yuelioi/bpi-py)，对外只返回普通 dict。SDK 出问题或要换掉时，只改这一个文件。

- bpi-py 负责：WBI 签名（搜索、playurl）、扫码登录的 cookie 处理、收藏夹接口。
- 直接请求：`view`（SDK 模型会丢掉 duration/desc/pubdate 等字段）、音乐中心榜单、UP 主投稿（SDK 没有覆盖）。
- bpi-py 的模型是 strict 校验，B 站改字段就会抛 `ResponseDecodeError`。适配层捕获后从异常里取原始响应体做宽松解析，不会因此失败。

**风控对策**（B 站会按 TLS 指纹、设备 id 和请求频率拦截）：

- 所有请求走 `curl_cffi` 模拟 Chrome 的 TLS 指纹；请求头里去掉 httpx/bpi-py 自带的 UA，由 curl_cffi 填入匹配的 Chrome UA。
- 启动时获取 `buvid3/buvid4` 设备 cookie 并持久化。没有它时搜索接口会返回 `v_voucher`（验证码软拦截）。
- 遇到 412 / -352 / `v_voucher`：先刷新设备 id 重试，之后按递增间隔重试。UP 主投稿接口随机拒绝的比例较高，重试 4 次。
- 并发上限 4，结果按接口缓存（详情 6 小时、UP 主 1 天、投稿 3 小时、榜单 0.5～1 小时）。
- `/x/space/wbi/arc/search` 即使模拟 Chrome 仍稳定返回 412，所以改用 `/x/series/recArchivesByKeywords`。

**播放。** 取 DASH 音频流，以 `StreamType.HTTP` 交给 MA 的 ffmpeg，带上 `Referer` 和浏览器 UA。流地址按 URL 里的 `deadline` 参数设置过期时间（约 2 小时）。

**图片。** B 站图床拒绝外站 Referer（返回 403），所以所有图片都设为 `remotely_accessible=False`，由 MA 服务端代理。视频封面会请求 CDN 的方形裁切版本（`@500w_500h_1c.jpg`）。

## 开发

测试需要 MA 本身，在 HiFi 使用的 MA 镜像里运行（会按 `manifest.json` 安装依赖）：

```bash
scripts/test_ma_providers.sh                      # 两个 provider，默认镜像
scripts/test_ma_providers.sh <image> bilibili       # 指定镜像和 provider
```

对真实服务的冒烟测试是 `scripts/providers/bilibili_live_smoke.py`，需要装有 MA 的 Python 环境运行
（可加 --cookie "SESSDATA=...; bili_jct=...; DedeUserID=..."）。

依赖版本：`curl_cffi` 锁定 0.16.3。0.17 要求 `certifi>=2026.10.19`，与 MA 锁定的 `certifi==2025.11.12` 冲突。

## 部署

由 HiFi 安装器负责：`scripts/install.sh … --with-music-assistant` 默认把它复制到
`MUSIC_ASSISTANT_CUSTOM_PROVIDERS_DIR`，MA 启动时加载（`--without-ma-provider bilibili` 跳过）。
然后在 MA 的「设置 → 音乐提供商 → 添加」里选 Bilibili。

## 已知限制 / 待办

- 登录 cookie 有效期约半年，尚未实现 refresh_token 自动续期；过期后需要重新扫码（MA 会提示重新认证）。
- 收藏夹只读（不支持在 MA 里添加或删除收藏）。
- 未实现：合集/系列作为歌单、关注列表作为艺人库、歌词、杜比音轨。
- 音乐中心「新歌速递」不返回 UP 主信息，每首都要查一次视频详情（有缓存）。
- `manifest.json` 的 `codeowners` 还是空的；如果要提交到 MA 官方仓库，需要填上 GitHub 用户名。
