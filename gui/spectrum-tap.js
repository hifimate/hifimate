/* Loaded synchronously before the host page's own scripts (Music Assistant).
 * Remembers every WebAudio node the page connects to an audio output, so the
 * spectrum overlay can listen in parallel. It never changes or removes any
 * connection, so playback is unaffected whether or not the overlay is shown. */
(() => {
  if (window.__hifimateAudioTap || typeof AudioNode === 'undefined') return;
  const outputs = [], listeners = new Set();
  const original = AudioNode.prototype.connect;
  const isOutput = node => node instanceof AudioDestinationNode
    || (typeof MediaStreamAudioDestinationNode !== 'undefined' && node instanceof MediaStreamAudioDestinationNode);
  AudioNode.prototype.connect = function connect(destination, ...rest) {
    const result = original.call(this, destination, ...rest);
    if (isOutput(destination) && !outputs.some(output => output.node === this)) {
      outputs.push({ node: this, context: this.context });
      listeners.forEach(listener => { try { listener(); } catch (_) { /* never break playback */ } });
    }
    return result;
  };
  window.__hifimateAudioTap = {
    outputs,
    // Overlay connections use the unwrapped connect, so they are not outputs.
    connect: (node, target) => original.call(node, target),
    subscribe(listener) { listeners.add(listener); return () => listeners.delete(listener); },
  };
})();
