// Capture mono PCM in the audio thread; all samples stay in memory.
class Recorder extends AudioWorkletProcessor {
  constructor() {
    super();
    this.active = true;
    this.remaining = sampleRate * 29;
    this.port.onmessage = ({data}) => {
      if (data === 'stop' && this.active) {
        this.active = false;
        this.port.postMessage({done: true});
      }
    };
  }
  process(inputs) {
    const channels = inputs[0];
    if (!this.active || !channels.length) return true;
    const count = Math.min(channels[0].length, this.remaining);
    const mono = new Float32Array(count);
    for (const channel of channels) {
      for (let i = 0; i < count; i++) mono[i] += channel[i] / channels.length;
    }
    this.port.postMessage({samples: mono}, [mono.buffer]);
    this.remaining -= count;
    if (!this.remaining) {
      this.active = false;
      this.port.postMessage({done: true});
    }
    return true;
  }
}
registerProcessor('pcm-recorder', Recorder);
