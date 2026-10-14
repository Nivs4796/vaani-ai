/* cues.js: Vaani's short chimes, synthesised in the page (no audio files). Contract: .build/CONTRACT-v3.md (CORE).
 *
 * Vaani.cues.play(name) -> true when the chime was scheduled. Names: wake, listen, ack, done,
 * stop, error, tick (one soft blip per countdown second), heads_up. Sine notes with the mock-up's
 * envelope (peak ~0.07, ~0.24 s per note, notes 0.1 s apart) on the page's one AudioContext
 * (Vaani.audio(), from voice.js), or a context of our own when that is missing. Silent while
 * Vaani is muted or the page is in Chat mode. Never throws: a chime is never worth an error.
 */
"use strict";
(() => {
  const V = window.Vaani;
  const NOTES = {
    wake: [660, 990], listen: [740], ack: [880], done: [784, 1175],
    stop: [523, 392], error: [330, 262], heads_up: [660, 880, 990], tick: [1000],
  };
  const PEAK = 0.07, NOTE_S = 0.24, GAP_S = 0.1;
  const TICK = {peak: 0.035, len: 0.09};  // the countdown blip: quieter and shorter than a chime
  const STALE_MS = 500;                   // a chime that waited this long for the context to resume is dropped
  let own = null;

  function silent() {
    try {
      if (V.hands && V.hands.muted) return true;
      if (V.mode === "chat") return true;
    } catch (_) { return true; }
    return false;
  }

  function context() {
    try {
      if (typeof V.audio === "function") {
        const ac = V.audio();
        if (ac) return ac;
      }
    } catch (_) { /* fall back to our own context */ }
    try {
      const AC = window.AudioContext || window.webkitAudioContext;
      if (!AC) return null;
      if (!own || own.state === "closed") own = new AC();
      return own;
    } catch (_) { return null; }
  }

  function schedule(ac, name) {
    const notes = NOTES[name];
    const peak = name === "tick" ? TICK.peak : PEAK;
    const len = name === "tick" ? TICK.len : NOTE_S;
    const t0 = ac.currentTime + 0.01;
    notes.forEach((f, i) => {
      const o = ac.createOscillator();
      const g = ac.createGain();
      const t = t0 + i * GAP_S;
      o.type = "sine";
      o.frequency.value = f;
      g.gain.setValueAtTime(0.0001, t);
      g.gain.exponentialRampToValueAtTime(peak, t + 0.02);
      g.gain.exponentialRampToValueAtTime(0.0001, t + len);
      o.connect(g);
      g.connect(ac.destination);
      o.onended = () => { try { o.disconnect(); g.disconnect(); } catch (_) { /* already gone */ } };
      o.start(t);
      o.stop(t + len + 0.02);
    });
  }

  function play(name) {
    try {
      if (!Object.prototype.hasOwnProperty.call(NOTES, name) || silent()) return false;
      const ac = context();
      if (!ac || ac.state === "closed") return false;
      if (ac.state === "suspended") {
        // Resume first; outside a user gesture this may never resolve, so a late chime is dropped.
        const asked = Date.now();
        Promise.resolve(ac.resume()).then(() => {
          try { if (Date.now() - asked <= STALE_MS && ac.state === "running" && !silent()) schedule(ac, name); } catch (_) { /* ignore */ }
        }).catch(() => {});
        return true;
      }
      schedule(ac, name);
      return true;
    } catch (_) { return false; }
  }

  V.cues = {play, names: Object.keys(NOTES)};
})();
