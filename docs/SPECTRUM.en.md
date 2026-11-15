[中文](SPECTRUM.md) | **English**

# Spectrum

How the spectrum shown on the control page and in the Music Assistant floating window is obtained, analysed and displayed. For the overall architecture see [ARCHITECTURE.en.md](ARCHITECTURE.en.md).

## 1. Data source

CamillaDSP is the official release, unpatched. The official release only exposes per-channel levels, not samples, so the spectrum is obtained like this:

1. **DSP input**: while someone has the spectrum open, `hifimate-spectrum.service` uses `pw-record` to create a passive (`node.passive`) PipeWire capture,
   targeting the device the DSP is capturing from at that moment (the monitor of the current lane; `bluez_input.*` for Bluetooth), at the DSP's sample rate, S32 stereo.
   The device and sample rate are read from the DSP's websocket (`GetConfigJson`) and follow changes of source or sample rate. No capture is made while the DSP is not running, and when nobody is watching the capture does not exist at all.
2. **DSP response**: the same CamillaDSP program is run offline on the current pipeline (filters, mixer, processors), with a file containing one impulse in each of the left and right channels as input,
   and the output written to a file; the running DSP and the DAC are not touched. The FFT of the output is the transfer matrix H[output][input] of the whole chain, with every filter type, FIR included, accounted for exactly.
   It is re-measured only when the pipeline or the sample rate changes (about 10 ms) and cached per configuration.
3. **Live state**: the master volume and mute (which sit before the pipeline) and the faders of Volume filters (such as Aux4 used for hand-over) are read from `GetFaders` and multiplied in.
4. **Alignment**: the analysis window is delayed by the DSP's playback buffer (`GetBufferLevel`, at most 0.5 s) to line up with what the DAC is playing right now.

What is displayed is the input spectrum × H × fader gain, and the frame's `origin` is `dsp-model`. Compared frame by frame with the earlier method of reading the ALSA output of a patched build, the long-term average difference is 0.06 dB
and the maximum 0.43 dB; the difference mainly comes from the different sampling instants of the two analysers. The only things it cannot see are the non-linear parts: clipping, quantization noise, compression and the dynamics of limiters.

## 2. Analysis

- DC removal and a Blackman window. All bands share one window length, the same as audioMotion's (8192 points at 48 kHz, about 171 ms; about 186 ms for the 44.1 kHz family).
- Frequency axis: the default is `log`, which is audioMotion's 1/24-octave, starting at 10 Hz and ending at the Nyquist frequency of the current sample rate (270 bands at 48 kHz, 318 at 192 kHz,
  366 at 768 kHz); `bark`, `linear` and `mel` correspond to audioMotion mode=1 with 240 equally spaced bands. Each band interpolates FFT bins at its lower and upper edges
  and takes the maximum of the bins between the edges.
- Two channels: `dual-combined`; the left and right channels are FFT'd separately and combined as (left power + right power) / 2, which avoids `max(L,R)` raising the noise floor and also avoids cancellation of out-of-phase signals.
- Amplitude: the same as audioMotion's linearAmplitude (-85…-25 dB, WebAudio units), with linearBoost 2.0.
- 30 frames per second are pushed, and the browser interpolates and draws at up to 60 frames per second; bars do not decay gradually, and peaks hold for 750 ms and then fall according to audioMotion's gravity.
- A change of capture stream (source, sample rate, reconnection) clears the analysis window; zero PCM received shows zero; with no new PCM for more than 350 ms the display is zeroed. The front end clears on a connection error, a bad frame or stale data
  rather than freezing the old picture.

## 3. Resources

- One FFT result is shared by all subscribers; the server only aggregates bands for the frequency axes currently being watched (each additional axis costs about 2.5 ms/frame more, at 48 kHz). There are at most 8 subscribers.
- The EventSource is closed when the spectrum is collapsed, the tab is hidden or the page is left; when the last subscriber disconnects, the capture process, the cache and the analysis thread are all released.
- When idle nothing is recomputed and no duplicate frames are sent; SSE sends at most one heartbeat every 0.5 s. The service runs with Nice=10 and does not request real-time scheduling; NumPy is required.
- The spectrum service calls no playback, volume or DSP configuration interface, so a spectrum fault does not affect playback.

## 4. Display

The front end (`gui/spectrum-overlay.js`) draws following audioMotion's `ledBars`: the LED height follows the bar width, is aligned to device pixels, and unlit cells are shown dark;
the vertical gradient is a fixed green-yellow-red, and for peaks only each bar's own cap is drawn. The horizontal axis labels are the ISO octave center frequencies, extended upwards at high sample rates; when the width is insufficient they are kept outward from 1 kHz on both sides.
The title bar switches the frequency axis, and the server re-aggregates for the selected scale (`/_hifimate/spectrum?frequencyScale=…`); the choice is stored in the browser.

The floating window on the MA page can be switched between "HiFiMate output" (「HiFiMate 输出」) and "This browser" (「本浏览器」). "This browser" works through `spectrum-tap.js`, which records the nodes the page connects to the WebAudio output destination;
once selected, it connects an `AnalyserNode` (`fftSize=8192`) in parallel, leaving the existing connections unchanged and not connecting SSE; WebAudio's 1/N scaling plus the Blackman window make a full-scale sine read
20·log10(0.42/2), so +13.56 dB is added to compensate. When MA switches to playing through an `<audio>` element, it shows "The current page is not playing" (「当前页面未在播放」).
