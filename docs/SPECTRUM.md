# 频谱

控制页和 Music Assistant 浮窗里的频谱怎样得到、怎样分析和显示。总体架构见 [ARCHITECTURE.md](ARCHITECTURE.md)。

## 1. 数据来源

CamillaDSP 用官方发布版，不打补丁。官方版只提供各声道的电平，不提供样本，所以频谱这样得到：

1. **DSP 的输入**：有人打开频谱时，`hifimate-spectrum.service` 用 `pw-record` 建一路被动（`node.passive`）的 PipeWire 采集，
   目标就是 DSP 此刻采集的设备（当前 lane 的 monitor，蓝牙是 `bluez_input.*`），用 DSP 的采样率、S32 立体声。
   设备和采样率从 DSP 的 websocket（`GetConfigJson`）读取，换来源、换采样率时跟着换。DSP 不运行时不采集，没人看时整路采集不存在。
2. **DSP 的响应**：用同一个 CamillaDSP 程序离线运行当前的 pipeline（滤波器、mixer、processor），输入是左、右声道各一个冲激的文件，
   输出写到文件，不碰运行中的 DSP 和 DAC。输出的 FFT 就是整条链的传递矩阵 H[输出][输入]，包括 FIR 在内的所有滤波器类型都精确包含。
   只在 pipeline 或采样率变化时重新测量（约 10 ms），按配置缓存。
3. **实时状态**：主音量、静音（在 pipeline 之前）和 Volume 滤波器的推子（如交接用的 Aux4）从 `GetFaders` 读取后乘上。
4. **对齐**：分析窗口按 DSP 的播放缓冲（`GetBufferLevel`，最多 0.5 秒）延后，与 DAC 正在播放的时刻对齐。

显示的是输入频谱 × H × 推子增益，帧的 `origin` 为 `dsp-model`。与早期打补丁读取 ALSA 输出的方法逐帧比较，长期平均差 0.06 dB，
最大 0.43 dB，差异主要来自两个分析器的取样时刻。看不到的只有非线性部分：削波、量化噪声、压缩和限幅器的动态。

## 2. 分析

- 去直流，Blackman 窗。所有频段共用一个与 audioMotion 相同的窗长（48 kHz 时 8192 点，约 171 ms；44.1 kHz 系列约 186 ms）。
- 频率轴：默认 `log`，即 audioMotion 的 1/24 倍频程，从 10 Hz 开始，止于当前采样率的 Nyquist（48 kHz 为 270 段，192 kHz 为 318 段，
  768 kHz 为 366 段）；`bark`、`linear`、`mel` 对应 audioMotion mode=1 的 240 个等距频带。频带对上下边缘做 FFT bin 插值，
  并取边缘之间 bin 的最大值。
- 双声道：`dual-combined`，左右分别做 FFT，以 (左功率 + 右功率) / 2 合成，避免 `max(L,R)` 抬高噪声底，也不会因反相抵消。
- 幅度：与 audioMotion 的 linearAmplitude 相同（-85…-25 dB，WebAudio 单位），linearBoost 2.0。
- 每秒推送 30 帧，浏览器以最高 60 帧/秒插值绘制；柱体不缓降，峰值停留 750 ms 后按 audioMotion 的 gravity 下落。
- 采集流切换（来源、采样率、重连）清空分析窗口；收到零 PCM 显示零；超过 350 ms 没有新 PCM 时清零。前端在连接错误、坏帧、数据过期时清空，
  不冻结旧画面。

## 3. 资源

- 同一份 FFT 结果供所有订阅者共享；服务端只为当前有人看的频率轴做频带聚合（多一种约多 2.5 ms/帧，48 kHz）。最多 8 个订阅者。
- 收起频谱、隐藏标签页或离开页面时关闭 EventSource；最后一个订阅者断开后，采集进程、缓存和分析线程一起释放。
- 空闲时不重复计算、不发送重复帧；SSE 每 0.5 秒最多一个心跳。服务以 Nice=10 运行，不申请实时调度；必须有 NumPy。
- 频谱服务不调用任何播放、音量或 DSP 配置接口，频谱出错不影响播放。

## 4. 显示

前端（`gui/spectrum-overlay.js`）按 audioMotion 的 `ledBars` 绘制：LED 高度跟随柱宽、按设备像素对齐，未点亮的格子以暗色显示；
固定的绿-黄-红纵向渐变，峰值只画每根柱子自己的柱顶帽。横轴标签是 ISO 倍频程中心频率，高采样率时向上延伸，宽度不够时从 1 kHz 向两侧保留。
标题栏可切换频率轴，服务端按所选坐标重新聚合（`/_hifimate/spectrum?frequencyScale=…`），选择保存在浏览器里。

MA 页面的浮窗可以在「HiFiMate 输出」和「本浏览器」之间切换。「本浏览器」由 `spectrum-tap.js` 记录页面连接到 WebAudio 输出终点的节点，
选择后并联一个 `AnalyserNode`（`fftSize=8192`），不改变原有连接，不连 SSE；WebAudio 的 1/N 缩放加 Blackman 窗使满幅正弦读数为
20·log10(0.42/2)，因此补偿 +13.56 dB。MA 改用 `<audio>` 元素播放时显示「当前页面未在播放」。
