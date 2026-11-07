/* Copies the microphone's float samples out of the audio thread unchanged. */
class Recorder extends AudioWorkletProcessor {
  process(inputs) {
    const channel = inputs[0] && inputs[0][0];
    if (channel) this.port.postMessage(channel.slice(0));
    return true;
  }
}
registerProcessor('hifimate-recorder', Recorder);
