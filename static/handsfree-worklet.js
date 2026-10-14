/* handsfree-worklet.js: the mic tap for voice mode (ENGINE, contract .build/CONTRACT-v3.md).
 *
 * Runs on the audio thread. It only copies the mic's first channel into blocks of BLOCK samples
 * and posts each block to handsfree.js, which does the VAD, the pre-roll ring and the WAV
 * encoding. Nothing is kept here and nothing is played (the output stays silent).
 */
"use strict";
(() => {
const BLOCK = 1024;   // ~21 ms at 48 kHz

class VaaniCapture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.buf = new Float32Array(BLOCK);
    this.n = 0;
  }

  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (!ch) return true;
    for (let i = 0; i < ch.length; i++) {
      this.buf[this.n++] = ch[i];
      if (this.n === BLOCK) {
        this.port.postMessage(this.buf, [this.buf.buffer]);
        this.buf = new Float32Array(BLOCK);
        this.n = 0;
      }
    }
    return true;
  }
}

registerProcessor("vaani-capture", VaaniCapture);
})();
