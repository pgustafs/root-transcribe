// Study notes: copies the shared tab's audio to the page, as mono blocks.
// Runs on the audio thread, so it keeps working while the recorder's browser
// tab is in the background. Blocks are batched (about a tenth of a second) to
// keep the message rate low.
class StudyTap extends AudioWorkletProcessor {
  constructor() {
    super();
    this.batch = new Float32Array(2048);
    this.filled = 0;
  }

  process(inputs) {
    const input = inputs[0];
    if (!input || input.length === 0) return true;
    const frames = input[0].length;
    for (let i = 0; i < frames; i += 1) {
      let sum = 0;
      for (let channel = 0; channel < input.length; channel += 1)
        sum += input[channel][i];
      this.batch[this.filled] = sum / input.length;
      this.filled += 1;
      if (this.filled === this.batch.length) {
        this.port.postMessage(this.batch, [this.batch.buffer]);
        this.batch = new Float32Array(2048);
        this.filled = 0;
      }
    }
    return true;
  }
}

registerProcessor("study-tap", StudyTap);
