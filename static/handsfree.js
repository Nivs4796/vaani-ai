/* handsfree.js: voice mode's hands-free engine (ENGINE). Contract: .build/CONTRACT-v3.md.
 *
 * One tap (wake) unlocks the page's AudioContext and opens the mic once. From then on, while the
 * page is in Voice mode, the mic is tapped (AudioWorklet in handsfree-worklet.js, ScriptProcessor
 * as a fallback) into a short pre-roll ring and an energy VAD with an adaptive noise floor. Speech
 * segments become 16 kHz mono WAVs encoded here:
 *   standby   -> POST /voice/wake (wake word only; anything else is dropped by the server)
 *   listening -> POST /voice/stt -> Vaani.turn("/voice/turn", {transcript_id}) (renders in the chat thread)
 *   speaking  -> barge-in: the playback ducks; "stop" cuts it, other speech becomes the next turn
 *   countdown -> speech holds the count until its transcript is parsed; only "stop" words count
 * Vaani's answers are WAVs from POST /voice/tts and GET /voice/cue/<name>, played through Web
 * Audio; every clip end is reported to POST /voice/played so the server knows what was heard.
 * Chat mode releases every mic track: no capture at all there.
 * Public: Vaani.hands = {wake, tapCore, stop, endSession, setMuted, muted, phase}.
 * Events: voice:phase, voice:heard, voice:caption, voice:focus, voice:level, voice:say, voice:mute.
 */
"use strict";
(() => {
const V = window.Vaani;

// ---------------------------------------------------------------- tuning
const TARGET_RATE = 16000;          // what /voice/stt and /voice/wake receive (16-bit mono WAV)
const PREROLL_MS = 300;             // audio kept from just before the speech onset
const CALIBRATE_MS = 400;           // no onset right after the mic opens: the noise floor settles first
const ONSET_MS = 80;                // this much loud audio in a row = speech, not a click
const VAD_ON_MIN = 0.012, VAD_ON_RATIO = 3;          // speech threshold: max(min, floor x ratio)
const VAD_BARGE_MIN = 0.045, VAD_BARGE_RATIO = 6;    // while Vaani speaks (echo leaks): much higher
const VAD_OFF_FACTOR = 0.6;                          // hysteresis: silence is below on x factor
const FLOOR_INIT = 0.006, FLOOR_MIN = 0.0015, FLOOR_MAX = 0.05;
const WAKE_MIN_MS = 300, WAKE_MAX_MS = 3000, WAKE_SILENCE_MS = 700;
const LISTEN_MAX_MS = 15000, SILENCE_MS = 900, MIN_UTTER_MS = 200;
const FOLLOWUP_MS = 8000;           // after Vaani speaks: no wake word needed for this long
const APPROVAL_MS = 30000;          // a read-back's approval window (READBACK_TTL_S on the server)
const DONE_HOLD_MS = 1500;          // "DONE · VERIFIED" stays this long, then the follow-up window
const ACK_AFTER_MS = 700, PROGRESS_AFTER_MS = 2500, PROGRESS_GAP_MS = 4000, STILL_WORKING_MS = 8000;
const HEADSUP_MIN = 10, HEADSUP_SLACK_MIN = 1, HEADSUP_POLL_MS = 60000;
const DUCK_GAIN = 0.3, COUNT_MIN_LEFT = 1, HELD_MAX_MS = 3000, HELD_RETRY_MS = 300;
const BUSY_WAIT_MS = 13000;         // a transcript is single use within 15 s
const OUT_TAIL_MS = 350;            // echo tail after a clip ends: captures still count as overlapping
const WARM_NOTE_MS = 20000;         // "warming up" is said at most this often

const HINT = {
  asleep: "Tap the core to wake Vaani",
  standby: "Say “Hey Vaani”, or tap the core",
  listening: "Listening · try “next”, “read it” or “reply saying …”",
  working: "Working on it…",
  speaking: "Say “stop” or tap the core to interrupt",
  approval: "Waiting for your voice · say one of the words on the card",
  countdown: "Say “stop” to cancel",
  done: "Done",
};
const LABEL = {asleep: "ASLEEP", standby: "STANDBY", listening: "LISTENING", working: "WORKING", speaking: "SPEAKING",
  approval: "AWAITING YOUR VOICE", countdown: "SENDING IN 5 · SAY STOP", done: "DONE · VERIFIED"};
const VERB = {"send it": "SENDING", "trash it": "DELETING", "delete it": "DELETING", "invite them": "INVITING",
  "move it": "MOVING", "accept it": "ACCEPTING", "decline it": "DECLINING", "say maybe": "REPLYING"};
const TOOL_CUE = {list_calendar_events: "checking_calendar", list_recent_emails: "reading_inbox", read_email: "opening_email",
  draft_email: "writing_draft", find_free_time: "looking_free_time", commit_pending: "saving"};
const STOPPED_CARD = {kind: "receipt", tone: "amber", label: "STOPPED · NOTHING SENT", title: "Not sent", sub: "Nothing left your account"};

// Local word checks (the server parses again; these only decide what the page does right now).
const STOP_RE = /^(?:stop|wait|hold on|hold it|hang on|cancel|no|nope|don't|do not|never ?mind|abort|halt|pause)(?: (?:it|that|this|please|now|vaani|talking|stop|wait|a sec|a second|a moment|a minute|there))*$/;
const FAST_RE = /^(?:confirm(?:ed)?|save it|send it|trash it|delete it|invite them|move it|accept it|decline it|say maybe|stop|wait|hold on|cancel|no|undo|repeat|say that again|good ?night(?: vaani)?|not now)$/;
function norm(t) {
  return String(t || "").toLowerCase().replace(/[’‘`]/g, "'").replace(/[^a-z0-9' ]+/g, " ").replace(/\s+/g, " ").trim()
    .replace(/^(?:(?:um+|uh+|er+|erm|oh|hey|okay|ok|please|vaani)\s+)+/, "").replace(/\s+(?:please|vaani)$/, "");
}
const isStop = t => { const n = norm(t); return !!n && STOP_RE.test(n); };
const isFast = t => FAST_RE.test(norm(t));

// ---------------------------------------------------------------- pure helpers (also used by offline checks)
function rmsOf(x) {
  let s = 0;
  for (let i = 0; i < x.length; i++) s += x[i] * x[i];
  return x.length ? Math.sqrt(s / x.length) : 0;
}

// Box-filter resampler: each output sample averages the input samples it covers (anti-aliasing enough for speech).
function resample(x, from, to) {
  if (!from || from === to) return x;
  const ratio = from / to, n = Math.floor(x.length / ratio), out = new Float32Array(n);
  for (let i = 0; i < n; i++) {
    const a = Math.floor(i * ratio), b = Math.min(x.length, Math.max(a + 1, Math.ceil((i + 1) * ratio)));
    let s = 0;
    for (let j = a; j < b; j++) s += x[j];
    out[i] = s / (b - a);
  }
  return out;
}

function encodeWav(x, rate) {
  const buf = new ArrayBuffer(44 + x.length * 2), dv = new DataView(buf);
  const str = (o, s) => { for (let i = 0; i < s.length; i++) dv.setUint8(o + i, s.charCodeAt(i)); };
  str(0, "RIFF"); dv.setUint32(4, 36 + x.length * 2, true); str(8, "WAVE");
  str(12, "fmt "); dv.setUint32(16, 16, true); dv.setUint16(20, 1, true); dv.setUint16(22, 1, true);
  dv.setUint32(24, rate, true); dv.setUint32(28, rate * 2, true); dv.setUint16(32, 2, true); dv.setUint16(34, 16, true);
  str(36, "data"); dv.setUint32(40, x.length * 2, true);
  for (let i = 0; i < x.length; i++) {
    const s = Math.max(-1, Math.min(1, x[i]));
    dv.setInt16(44 + i * 2, s < 0 ? s * 0x8000 : s * 0x7fff, true);
  }
  return buf;
}

function concat(chunks, total) {
  const out = new Float32Array(total);
  let o = 0;
  for (const c of chunks) { if (o + c.length > total) break; out.set(c, o); o += c.length; }
  return out.subarray(0, o);
}

// Energy VAD over blocks of audio. push(rms, ms, {barge, silenceMs, maxMs}) -> "onset" | "end" | "max" | "".
// The noise floor follows the room while nobody speaks: down fast, up slowly.
class Vad {
  constructor() { this.floor = FLOOR_INIT; this.age = 0; this.run = 0; this.inSpeech = false; this.silence = 0; this.len = 0; this.voiced = 0; }
  thresholds(barge) {
    const on = barge ? Math.max(VAD_BARGE_MIN, this.floor * VAD_BARGE_RATIO) : Math.max(VAD_ON_MIN, this.floor * VAD_ON_RATIO);
    return {on, off: on * VAD_OFF_FACTOR};
  }
  adapt(r, on) {
    const k = r < this.floor ? 0.2 : r < on ? 0.02 : 0.002;
    this.floor = Math.min(FLOOR_MAX, Math.max(FLOOR_MIN, this.floor + (r - this.floor) * k));
  }
  push(r, ms, o) {
    this.age += ms;
    const {on, off} = this.thresholds(o && o.barge);
    if (!this.inSpeech) {
      this.adapt(r, on);
      if (this.age < CALIBRATE_MS) { this.run = 0; return ""; }
      if (r <= on) { this.run = 0; return ""; }
      this.run += ms;
      if (this.run < ONSET_MS) return "";
      this.inSpeech = true; this.len = this.run; this.voiced = this.run; this.silence = 0; this.run = 0;
      return "onset";
    }
    this.len += ms;
    if (r > off) { this.silence = 0; this.voiced += ms; } else this.silence += ms;
    if (this.len >= o.maxMs) { this.inSpeech = false; return "max"; }
    if (this.silence >= o.silenceMs) { this.inSpeech = false; return "end"; }
    return "";
  }
  cancel() { this.inSpeech = false; this.run = 0; this.silence = 0; }
}

// ---------------------------------------------------------------- state
let phase = "asleep", label = LABEL.asleep, count = null;
let unlocked = false, waking = null, wakeReady = true;
let muted = false;
try { muted = localStorage.getItem("vaani.voice.muted") === "1"; } catch (_) { /* not saved */ }
let stream = null, srcNode = null, tapNode = null, sinkNode = null, inRate = 48000, micOpening = null, workletCtx = null;
let vad = null, ring = [], ringSamples = 0, cap = null, sttInflight = 0;
let playing = null, cueP = null, outTail = 0, speakSeq = 0, cueSeq = 0, speakAbort = null, lastBody = null, stopWhy = "stopped";
let chimeUntil = 0, lastCaption = "", micLevel = 0, levelMic = 0, levelOut = 0, raf = 0;
let windowUntil = 0, windowTimer = 0, doneTimer = 0;
let cd = null, cdClock = 0, vturn = null, skipSpeak = false, pendingDraft = null;
let warnedAt = 0, headsTimer = 0, chain = Promise.resolve();
const announced = new Set();
try { for (const k of JSON.parse(sessionStorage.getItem("vaani.headsup") || "[]")) announced.add(String(k)); } catch (_) { /* fresh */ }

function stopTracks(s) {
  try { for (const t of (s && s.getTracks ? s.getTracks() : [])) { try { t.stop(); } catch (_) { /* gone */ } } } catch (_) { /* not a MediaStream */ }
}
const csrf = () => (V.me && V.me.csrf) || "";
const sleep = ms => new Promise(ok => setTimeout(ok, ms));
const sidQ = () => `session_id=${encodeURIComponent(V.sessionId || "")}`;
function isVoice() {
  if (typeof V.mode === "string") return V.mode === "voice";
  return document.documentElement.classList.contains("mode-voice");
}
function audio() { try { return typeof V.audio === "function" ? V.audio() : null; } catch (_) { return null; } }

// ---------------------------------------------------------------- events
function setPhase(p, opts = {}) {
  phase = p;
  label = opts.label || LABEL[p] || p.toUpperCase();
  count = p === "countdown" ? (opts.count != null ? opts.count : count) : null;
  clearTimeout(doneTimer);
  if (p === "listening") openWindow(FOLLOWUP_MS);
  else if (p === "approval") openWindow(APPROVAL_MS);
  else closeWindow();
  if (p === "done") armDone();
  const ev = {phase: p, label, hint: opts.hint || (p === "standby" && !wakeReady ? "Tap the core to talk" : HINT[p])};
  if (p === "countdown") { ev.count = count; if (opts.held) ev.held = true; }
  V.emit("voice:phase", ev);
}
function armDone() {
  clearTimeout(doneTimer);
  doneTimer = setTimeout(() => {
    if (phase !== "done") return;
    if (playing || speakAbort || vturn || cap) { armDone(); return; }
    setPhase("listening");
  }, DONE_HOLD_MS);
}
const heard = (text, extra) => V.emit("voice:heard", Object.assign({text: text || ""}, extra || {}));
function caption(text) { lastCaption = text || ""; V.emit("voice:caption", {text: lastCaption}); }
const focus = card => V.emit("voice:focus", {card: card || null});
function say(who, text, tag, tone) {
  const ev = {who, text};
  if (tag) ev.tag = tag;
  if (tone) ev.tone = tone;
  V.emit("voice:say", ev);
}
function chime(name) {
  if (muted) return;
  chimeUntil = performance.now() + 400;      // a chime leaking into the mic is not speech
  try { if (V.cues && typeof V.cues.play === "function") V.cues.play(name); } catch (e) { console.warn("Vaani: cue failed", e); }
}
function notice(text, tone) {
  caption(text);
  try { if (typeof V.toast === "function") V.toast(text, tone ? {tone} : {}); } catch (_) { /* the caption shows it */ }
}

// The follow-up window (listening) and the approval window: checked on every mic block (not throttled in
// a background tab) and by a timer (covers a mic that delivers nothing).
function openWindow(ms) {
  windowUntil = performance.now() + ms;
  clearTimeout(windowTimer);
  windowTimer = setTimeout(checkWindow, ms + 100);
}
function closeWindow() { windowUntil = 0; clearTimeout(windowTimer); }
function checkWindow() {
  if (!windowUntil || cap || sttInflight || vturn) return;
  if (performance.now() < windowUntil) return;
  if (phase === "listening" || phase === "approval") { setPhase("standby"); caption(""); heard(""); }
}

// ---------------------------------------------------------------- mic
async function openMic() {
  if (stream) return true;
  if (micOpening) return micOpening;
  micOpening = (async () => {
    const md = navigator.mediaDevices;
    if (!md || !md.getUserMedia) { notice("This browser cannot use the microphone on this page.", "warn"); return false; }
    let s;
    try {
      s = await md.getUserMedia({audio: {echoCancellation: true, noiseSuppression: true, autoGainControl: true}});
    } catch (e) {
      const n = e && e.name;
      notice(n === "NotAllowedError" || n === "SecurityError" ? "Microphone blocked: allow it in the address bar, then tap the core."
        : n === "NotFoundError" ? "No microphone found." : "Could not open the microphone.", "risk");
      return false;
    }
    if (!isVoice()) { stopTracks(s); return false; }   // switched to Chat meanwhile
    stream = s;
    try { await attachTap(s); } catch (e) { console.warn("Vaani: mic tap failed", e); }
    return !!stream;
  })();
  try { return await micOpening; } finally { micOpening = null; }
}

async function attachTap(s) {
  const ctx = audio();
  if (!ctx) return;
  inRate = ctx.sampleRate || 48000;
  let src;
  try { src = ctx.createMediaStreamSource(s); }
  catch (e) { console.warn("Vaani: the mic stream has no audio to tap", e); return; }   // e.g. a fake stream without audio
  let node = null;
  if (ctx.audioWorklet && window.AudioWorkletNode) {
    try {
      if (workletCtx !== ctx) { await ctx.audioWorklet.addModule("/static/handsfree-worklet.js"); workletCtx = ctx; }
      node = new AudioWorkletNode(ctx, "vaani-capture", {numberOfInputs: 1, numberOfOutputs: 1, outputChannelCount: [1],
        channelCount: 1, channelCountMode: "explicit"});
      node.port.onmessage = e => onBlock(e.data);
    } catch (e) { console.warn("Vaani: AudioWorklet unavailable, using ScriptProcessor", e); node = null; }
  }
  if (!node && ctx.createScriptProcessor) {
    node = ctx.createScriptProcessor(2048, 1, 1);
    node.onaudioprocess = e => onBlock(new Float32Array(e.inputBuffer.getChannelData(0)));
  }
  if (!node || stream !== s) { try { src.disconnect(); } catch (_) { /* never connected */ } return; }   // released meanwhile
  const sink = ctx.createGain();
  sink.gain.value = 0;                     // pulled by the graph, never heard
  src.connect(node); node.connect(sink); sink.connect(ctx.destination);
  srcNode = src; tapNode = node; sinkNode = sink;
  vad = new Vad(); ring = []; ringSamples = 0;
  meter();
}

function releaseMic() {
  abortCapture();
  stopTracks(stream);
  stream = null;
  if (tapNode) { if (tapNode.port) tapNode.port.onmessage = null; tapNode.onaudioprocess = null; }
  for (const n of [srcNode, tapNode, sinkNode]) { try { if (n) n.disconnect(); } catch (_) { /* gone */ } }
  srcNode = tapNode = sinkNode = null; vad = null; ring = []; ringSamples = 0; micLevel = 0;
}

function abortCapture() {
  const c = cap;
  cap = null;
  if (vad) vad.cancel();
  if (c && c.purpose === "barge") duck(false);
  if (c && c.purpose === "count") releaseHold();
}

// ---------------------------------------------------------------- capture loop (one block of mic audio)
function onBlock(x) {
  if (!vad || !stream || !(x instanceof Float32Array) || !x.length) return;
  if (!isVoice()) return;
  const now = performance.now();
  const ms = x.length / inRate * 1000;
  const r = rmsOf(x);
  micLevel = Math.min(1, Math.max(0, r - vad.floor) / 0.12);
  const outNow = !!playing || !!cueP || now < outTail;

  ring.push(x); ringSamples += x.length;
  const keep = Math.round(PREROLL_MS / 1000 * inRate);
  while (ring.length > 1 && ringSamples - ring[0].length >= keep) ringSamples -= ring.shift().length;
  if (cap) { cap.chunks.push(x); cap.samples += x.length; if (outNow) cap.overlap = true; }

  if (cd) countClock(now);
  if (windowUntil && !cap) checkWindow();
  if (phase === "asleep") return;

  const purpose = cap ? cap.purpose : phase === "standby" ? "wake" : phase === "countdown" ? "count" : playing ? "barge" : "command";
  const wake = purpose === "wake";
  // A wake segment runs until the speaker stops (not just 3 s), so "Hey Vaani, <a long request>" keeps
  // its request; only the first WAKE_MAX_MS ever goes to /voice/wake (onEnd).
  const ev = vad.push(r, ms, {barge: !!playing || !!cueP || now < chimeUntil, silenceMs: wake ? WAKE_SILENCE_MS : SILENCE_MS, maxMs: LISTEN_MAX_MS});
  if (ev === "onset") onOnset(purpose, outNow, now);
  else if ((ev === "end" || ev === "max") && cap) onEnd();
}

function onOnset(purpose, outNow, now) {
  cap = {purpose, chunks: ring.slice(), samples: ringSamples, t0: now, overlap: outNow};
  if (purpose === "count") holdCountdown();
  else if (purpose === "barge") duck(true);
  else if (purpose === "command") { caption(""); heard(""); }
}

function onEnd() {
  const c = cap;
  cap = null;
  if (!c) return;
  const voiced = vad ? vad.voiced : 0;
  if (voiced < (c.purpose === "wake" ? WAKE_MIN_MS : MIN_UTTER_MS)) {   // a cough or a click
    if (c.purpose === "barge") duck(false);
    if (c.purpose === "count") releaseHold();
    return;
  }
  const pcm = resample(concat(c.chunks, c.samples), inRate, TARGET_RATE);
  const wavOf = x => new Blob([encodeWav(x, TARGET_RATE)], {type: "audio/wav"});
  if (c.purpose === "wake") {
    const head = WAKE_MAX_MS / 1000 * TARGET_RATE;   // the wake check only ever gets the first 3 s
    const full = pcm.length > head + TARGET_RATE / 4 ? wavOf(pcm) : null;   // longer: kept for after the wake word matched
    onWakeAudio(wavOf(pcm.subarray(0, head)), full, c.overlap).catch(e => console.warn("Vaani: wake failed", e));
    return;
  }
  handleCapture(c, wavOf(pcm)).catch(e => console.warn("Vaani: capture failed", e));
}

async function postAudio(path, wav, overlap) {
  if (!V.sessionId) return null;
  const headers = {"Content-Type": "audio/wav", "X-CSRF-Token": csrf()};
  if (overlap) headers["X-Vaani-Overlap"] = "1";
  let res, j;
  try {
    res = await fetch(`${path}?${sidQ()}`, {method: "POST", body: wav, headers, credentials: "same-origin"});
    j = await res.json().catch(() => ({}));
  } catch (_) { warn("Voice could not reach Vaani on this Mac."); return null; }
  if (res.status === 401) { location.href = "/login"; return null; }
  if (res.status === 503) { warn("My speech model is still warming up. Try again in a moment."); return null; }
  if (!res.ok) { console.warn(`Vaani: ${path} failed (${res.status})`, j && j.error); return null; }
  return j;
}
function warn(text) {
  const now = performance.now();
  if (now - warnedAt < WARM_NOTE_MS) return;
  warnedAt = now;
  caption(text);
}

async function handleCapture(c, wav) {
  sttInflight++;
  let j;
  try { j = await postAudio("/voice/stt", wav, c.overlap); }
  finally { sttInflight--; }
  if (!isVoice()) return;
  const text = String((j && j.text) || "").trim();
  if (!j || !text || j.empty || !j.tid) {            // nothing usable: carry on quietly
    if (c.purpose === "barge") duck(false);
    if (c.purpose === "count") releaseHold();
    if (phase === "listening" || phase === "approval") openWindow(phase === "approval" ? APPROVAL_MS : FOLLOWUP_MS);
    return;
  }
  const meta = {tid: j.tid, confidence: j.confidence == null ? null : j.confidence};
  if (c.purpose === "count") return onCountWords(text, meta);
  if (c.purpose === "barge" || (playing && c.overlap)) {
    if (isStop(text)) { heard(text, meta); stopSpeech(); enqueueTurn(j.tid, text, {stop: true}); return; }
    duck(false);
    if (isEcho(text)) return;                        // Vaani heard itself
    heard(text, meta);
    enqueueTurn(j.tid, text, {afterSpeech: true});   // runs once Vaani has finished this answer
    return;
  }
  heard(text, meta);
  if (vturn && isStop(text)) skipSpeak = true;        // the running turn finishes but is not read out
  enqueueTurn(j.tid, text, {stop: isStop(text)});
}

// Vaani's own words coming back through the speaker: most of the words are in what it is saying.
function isEcho(text) {
  const said = new Set(norm(lastCaption).split(" ").filter(Boolean));
  const words = norm(text).split(" ").filter(w => w.length > 1);
  if (!said.size || !words.length) return false;
  const hits = words.filter(w => said.has(w)).length;
  return hits / words.length >= 0.7;
}

async function onWakeAudio(wav, full, overlap) {
  const j = await postAudio("/voice/wake", wav, false);
  if (!j || !j.wake || !isVoice() || phase !== "standby") return;
  chime("listen");
  if (full) {  // the request ran past the wake clip: the whole utterance goes through /voice/stt like any command
    setPhase("listening");
    await handleCapture({purpose: "command", overlap: !!overlap}, full);
    return;
  }
  const rest = String(j.rest_text || "").trim();
  if (rest && j.rest_tid) {
    heard(rest, {tid: j.rest_tid, confidence: j.confidence == null ? null : j.confidence});
    enqueueTurn(j.rest_tid, rest, {stop: isStop(rest)});
  } else {
    caption(""); heard("");
    setPhase("listening");
  }
}

// ---------------------------------------------------------------- playback (speech channel + cue channel)
function savedVoice() { try { return localStorage.getItem("vaani.voice.name") || ""; } catch (_) { return ""; } }
function decodeHeader(v) { try { return v ? decodeURIComponent(v) : ""; } catch (_) { return v || ""; } }

function played(speakId, complete) {
  if (!speakId || !V.sessionId) return;
  Promise.resolve().then(() => V.api("/voice/played", {session_id: V.sessionId, speak_id: speakId, complete: !!complete}))
    .catch(e => console.warn("Vaani: /voice/played failed", e));
}

function decodeAudio(ctx, ab) {
  if (V.voiceKit && V.voiceKit.decode) return V.voiceKit.decode(ctx, ab);
  return new Promise((ok, no) => { const p = ctx.decodeAudioData(ab, ok, no); if (p && p.then) p.then(ok, no); });
}

// Fetches one clip. Returns {buf, speakId, text} | {status} (refused / silent / failed) | null (stopped).
async function fetchClip(url, init, isCurrent) {
  const ctx = audio();
  let res, ab, speakId = null, text = "";
  try {
    res = await fetch(url, Object.assign({credentials: "same-origin"}, init));
    speakId = res.headers.get("X-Vaani-Speak-Id");
    text = decodeHeader(res.headers.get("X-Vaani-Spoken"));
    if (res.status === 401) { location.href = "/login"; return {status: "failed"}; }
    if (res.status === 204) return {status: "silent"};
    if (res.status === 409) return {status: "refused"};
    if (!res.ok) return {status: "failed"};
    ab = await res.arrayBuffer();
  } catch (e) {
    if (speakId) played(speakId, false);
    return e && e.name === "AbortError" ? null : {status: "failed"};
  }
  if (!isCurrent()) { played(speakId, false); return null; }
  if (!ctx || !ab || !ab.byteLength) { played(speakId, false); return {status: "failed"}; }
  let buf;
  try { buf = await decodeAudio(ctx, ab); } catch (_) { played(speakId, false); return {status: "failed"}; }
  if (ctx.state !== "running") {
    try { await Promise.race([Promise.resolve(ctx.resume()).catch(() => {}), sleep(400)]); } catch (_) { /* below */ }
  }
  if (!isCurrent()) { played(speakId, false); return null; }
  if (ctx.state !== "running") { played(speakId, false); return {status: "failed"}; }   // audio still locked
  return {buf, speakId, text};
}

// Plays a decoded clip: source -> gain (ducking) -> analyser (level) -> speakers.
function playOn(channel, clip) {
  return new Promise(resolve => {
    const ctx = audio();
    let p;
    try {
      const src = ctx.createBufferSource(), gain = ctx.createGain(), an = ctx.createAnalyser();
      an.fftSize = 1024;
      src.buffer = clip.buf;
      src.connect(gain); gain.connect(an); an.connect(ctx.destination);
      p = {src, gain, an, data: new Float32Array(an.fftSize), speakId: clip.speakId, channel, resolve, ended: false};
      src.onended = () => finishClip(p, true);
      // A context that stalls never fires onended: the guard ends the clip as not heard in full.
      p.guard = setTimeout(() => finishClip(p, ctx.state === "running"), clip.buf.duration * 1000 + 2000);
      if (channel === "say") playing = p; else cueP = p;
      src.start();
    } catch (e) {
      console.warn("Vaani: playback failed", e);
      if (p) finishClip(p, false, "failed"); else { played(clip.speakId, false); resolve("failed"); }
      return;
    }
    meter();
  });
}

function finishClip(p, complete, result) {
  if (!p || p.ended) return;
  p.ended = true;
  clearTimeout(p.guard);
  p.src.onended = null;
  if (!complete) { try { p.src.stop(); } catch (_) { /* not started */ } }
  try { p.src.disconnect(); p.gain.disconnect(); p.an.disconnect(); } catch (_) { /* gone */ }
  if (playing === p) playing = null;
  if (cueP === p) cueP = null;
  outTail = performance.now() + OUT_TAIL_MS;
  played(p.speakId, complete);
  p.resolve(result || (complete ? "done" : "stopped"));
}

// Speaks one /voice/tts request on the speech channel (replacing anything speaking). Resolves "done",
// "stopped" (someone else took over: the caller must not change the phase), "muted", "refused",
// "silent" or "failed". opts: phase/label (shown while speaking; omit to keep the phase), caption.
async function speak(body, opts = {}) {
  stopSpeech();
  const my = ++speakSeq;
  if (opts.phase) setPhase(opts.phase, {label: opts.label});
  heard("");
  if (opts.caption) caption(opts.caption);
  if (muted) return "muted";                     // captions only; nothing played, so nothing counts as heard
  if (!V.sessionId) return "failed";
  for (let t = 0; cueP && t < 1500 && my === speakSeq; t += 100) await sleep(100);   // let a short cue finish
  if (my !== speakSeq) return stopWhy;
  const full = Object.assign({session_id: V.sessionId}, body);
  const voice = savedVoice();
  if (voice && !full.voice) full.voice = voice;
  lastBody = full;
  const ctl = new AbortController();
  speakAbort = ctl;
  const clip = await fetchClip("/voice/tts", {method: "POST", signal: ctl.signal,
    headers: {"Content-Type": "application/json", "X-CSRF-Token": csrf()}, body: JSON.stringify(full)}, () => my === speakSeq);
  if (speakAbort === ctl) speakAbort = null;
  if (!clip) return my === speakSeq ? "failed" : stopWhy;
  if (clip.status) return my === speakSeq ? clip.status : stopWhy;
  if (my !== speakSeq) { played(clip.speakId, false); return stopWhy; }
  if (clip.text && !opts.caption) caption(clip.text);
  if (muted) { played(clip.speakId, false); return "muted"; }
  return playOn("say", clip);
}

function stopSpeech(result) {
  speakSeq++;
  stopWhy = result || "stopped";
  if (speakAbort) { try { speakAbort.abort(); } catch (_) { /* settled */ } speakAbort = null; }
  if (playing) finishClip(playing, false, result);
}

function stopCue() {
  cueSeq++;
  if (cueP) finishClip(cueP, false);
}

// A server phrase cue ("On it.", "Checking your calendar."): never talks over speech.
async function playCue(name) {
  if (muted || !name || !V.sessionId || playing || cueP || !isVoice()) return "skipped";
  const my = ++cueSeq;
  const clip = await fetchClip(`/voice/cue/${encodeURIComponent(name)}?${sidQ()}`, {method: "GET"},
    () => my === cueSeq && !playing && !muted);
  if (!clip || clip.status) return "skipped";
  if (my !== cueSeq || playing || cueP) { played(clip.speakId, false); return "skipped"; }
  return playOn("cue", clip);
}

function duck(on) {
  const p = playing, ctx = audio();
  if (!p || !ctx) return;
  try { p.gain.gain.setTargetAtTime(on ? DUCK_GAIN : 1, ctx.currentTime, 0.04); }
  catch (_) { p.gain.gain.value = on ? DUCK_GAIN : 1; }
}

// "repeat": the last request again (a new, recorded clip).
async function replay() {
  if (!lastBody) return "silent";
  const body = Object.assign({}, lastBody);
  delete body.session_id;
  return speak(body, {phase: body.readback_draft_id ? "approval" : "speaking", label: body.readback_draft_id ? "READING IT BACK" : null,
    caption: lastCaption});
}

// ---------------------------------------------------------------- levels (per animation frame)
function meter() { if (!raf && isVoice()) raf = requestAnimationFrame(frame); }
function frame() {
  raf = 0;
  const p = playing || cueP;
  let out = 0;
  if (p) {
    try { out = V.voiceKit && V.voiceKit.rms ? V.voiceKit.rms(p.an, p.data) : 0.3; } catch (_) { out = 0.3; }
    out = Math.min(1, out / 0.18);
  }
  const mic = stream ? micLevel : 0;
  levelMic += (mic - levelMic) * (mic > levelMic ? 0.5 : 0.15);
  levelOut += (out - levelOut) * (out > levelOut ? 0.5 : 0.12);
  if (levelMic < 0.003) levelMic = 0;
  if (levelOut < 0.003) levelOut = 0;
  V.emit("voice:level", {mic: levelMic, out: levelOut});
  if (isVoice() && (stream || playing || cueP || levelMic || levelOut)) raf = requestAnimationFrame(frame);
}

// ---------------------------------------------------------------- turns
function waitIdle(ms) {
  const t0 = performance.now();
  return new Promise(done => {
    (function check() { if ((!V.busy && V.sessionId) || performance.now() - t0 > ms) done(); else setTimeout(check, 120); })();
  });
}

function waitQuiet(ms) {
  const t0 = performance.now();
  return new Promise(done => {
    (function check() { if ((!playing && !speakAbort) || performance.now() - t0 > ms) done(); else setTimeout(check, 120); })();
  });
}

// Turns run one after another: a "stop" said while a turn runs waits for it, it is never dropped.
function enqueueTurn(tid, text, meta) {
  const job = chain.then(() => runTurn(tid, text, meta || {})).catch(e => console.warn("Vaani: voice turn failed", e));
  chain = job;
  return job;
}

async function runTurn(tid, text, meta) {
  if (!isVoice()) return;
  const t0 = performance.now();
  let r = null, started = false, failed = null, retriedMode = false;
  const mine = e => e && e.path === "/voice/turn" && e.payload && e.payload.transcript_id === tid;
  const offs = [V.on("turn:start", e => { if (mine(e)) started = true; }), V.on("turn:error", e => { if (mine(e)) failed = e.error || {}; })];
  try {
    for (;;) {
      await waitIdle(Math.max(0, BUSY_WAIT_MS - (performance.now() - t0)));
      if (meta.afterSpeech) await waitQuiet(Math.max(0, BUSY_WAIT_MS - 4000 - (performance.now() - t0)));
      if (!isVoice()) return;
      started = false; failed = null;
      beginWork(text, !!meta.stop);
      r = await V.turn("/voice/turn", {transcript_id: tid}, text, {via: "voice"});
      if (r) break;
      if (started && failed && failed.status === 409 && /voice mode/i.test(failed.message || "") && !retriedMode) {
        retriedMode = true;                         // the server missed the switch to Voice: tell it again, once
        try { await V.api("/voice/mode", {session_id: V.sessionId, mode: "voice"}); continue; } catch (_) { break; }
      }
      if (started || performance.now() - t0 > BUSY_WAIT_MS) break;
      await sleep(150);                             // lost the race for the busy flag: wait for that turn
    }
  } finally { offs.forEach(off => off()); endWork(); }
  if (!r) {
    skipSpeak = false;
    if (meta.stop && meta.arm) disarm("stop", {quiet: true});   // the server never heard the stop: disarm directly
    if (!isVoice()) return;
    if (!meta.stop) {
      chime("error");
      caption(started ? "Sorry, that did not work. Try again." : "Vaani is still busy. Say it again in a moment.");
    }
    if (phase === "working" || phase === "done") setPhase("listening");
    return;
  }
  await onResult(r, {stop: !!meta.stop});
}

function beginWork(text, isStopTurn) {
  endWork();
  const now = performance.now();
  vturn = {t0: now, fast: isStopTurn || isFast(text), acked: false, ackAt: 0, lastProgress: -Infinity, lastStepAt: now, tool: "", spoken: new Set(), timer: 0};
  vturn.timer = setInterval(workTick, 250);
  if (phase !== "countdown" && !isStopTurn) setPhase("working");
}
function endWork() { if (vturn) clearInterval(vturn.timer); vturn = null; }

function workTick() {
  const w = vturn;
  if (!w || !isVoice()) return;
  const now = performance.now(), el = now - w.t0;
  if (!w.acked && !w.fast && el >= ACK_AFTER_MS && !playing) {
    w.acked = true; w.ackAt = now;
    playCue("on_it");
    return;
  }
  if (el < PROGRESS_AFTER_MS || now - w.lastProgress < PROGRESS_GAP_MS || now - w.ackAt < 1500 || playing || cueP) return;
  let name = null;
  if (w.tool && !w.spoken.has(w.tool)) { name = TOOL_CUE[w.tool] || "still_working"; w.spoken.add(w.tool); }
  else if (now - w.lastStepAt >= STILL_WORKING_MS && now - w.lastProgress >= STILL_WORKING_MS) name = "still_working";
  if (!name) return;
  w.lastProgress = now;
  playCue(name);
}

function onStep(ev) {
  const w = vturn;
  if (!w || !ev || ev.phase === "end") return;
  w.tool = String(ev.tool || "");
  w.lastStepAt = performance.now();
  const step = String(ev.label || "").replace(/[….]+$/, "").trim().toUpperCase();
  if (phase === "working") setPhase("working", {label: step ? `WORKING · ${step}` : "WORKING"});
}

function receiptOk(r) {
  const rc = r && r.receipt;
  if (!rc) return false;
  const verified = typeof rc.verified === "number" ? (rc.count ? rc.verified >= rc.count : rc.verified > 0) : rc.verified === true;
  return verified && !/ERROR|FAIL|NOT/i.test(rc.status || "");
}
const stoppedResult = v => !!v && ((v.focus && /STOPPED/i.test(v.focus.label || "")) || v.intent === "stop" && !!v.focus);

function afterSpeech(next) {
  if (!isVoice()) return;
  if (next === "approval") setPhase("approval");
  else if (next === "standby") { setPhase("standby"); caption(""); }
  else if (next === "done") setPhase("done");
  else setPhase("listening");
}

// One /voice/turn, /voice/commit or /voice/disarm result: focus card, speech, then the next phase.
async function onResult(r, src = {}) {
  if (!isVoice() || !r) return;
  const v = r.voice || null;
  if (skipSpeak) { skipSpeak = false; if (v && v.focus) focus(v.focus); setPhase("listening"); return; }
  if (!v) {
    const out = await speak({result: r}, {phase: "speaking"});
    if (out !== "stopped") afterSpeech("listening");
    return;
  }
  if (v.focus) focus(v.focus);
  else if (/^(cancel|goodnight)$/.test(v.intent || "")) focus(null);
  const stopped = stoppedResult(v);
  if (stopped && cd) dropCountdown();
  const next = src.after || v.phase || "listening";

  switch (v.client) {
    case "stop_speaking":
      stopSpeech(); stopCue();
      afterSpeech(next === "standby" ? "standby" : next === "approval" ? "approval" : "listening");
      return;
    case "repeat": {
      const out = await replay();
      if (out !== "stopped") afterSpeech(next);
      return;
    }
    case "goodnight": {
      const out = v.spoken ? await speak({text: v.spoken}, {phase: "speaking", caption: v.spoken}) : "silent";
      if (out !== "stopped") { focus(null); afterSpeech("standby"); }
      return;
    }
    case "switch_chat": {
      if (v.spoken) await speak({text: v.spoken}, {phase: "speaking", caption: v.spoken});
      if (typeof V.setMode === "function") { try { await V.setMode("chat", {why: "voice"}); } catch (e) { console.warn("Vaani: switch to chat failed", e); } }
      return;
    }
    default: break;
  }

  if (v.countdown && v.countdown.arm_id) { startCountdown(v.countdown, v); return; }
  if (stopped) { if (!src.stop) chime("stop"); }
  else if (v.approved || receiptOk(r)) chime("done");
  if (src.quiet) { caption(v.spoken || ""); afterSpeech(next); return; }

  let out;
  if (v.readback && v.readback.draft_id) {
    out = await speak({readback_draft_id: v.readback.draft_id}, {phase: "approval", label: "READING IT BACK", caption: v.spoken || ""});
  } else if (v.spoken) {
    const look = stopped ? {phase: "done", label: "STOPPED"} : (v.approved || receiptOk(r)) ? {phase: "done", label: "DONE · VERIFIED"}
      : {phase: "speaking", label: "SPEAKING"};
    out = await speak({text: v.spoken}, Object.assign({caption: v.spoken}, look));
  } else out = "silent";
  if (out !== "stopped") afterSpeech(next);
}

// ---------------------------------------------------------------- countdown (tier 2: "send it" and friends)
function startCountdown(c, v) {
  dropCountdown();
  const secs = Math.max(1, Math.round(Number(c.seconds) || 5));
  cd = {arm: c.arm_id, draft: c.draft_id, word: c.word || "", verb: VERB[c.word] || "GOING AHEAD", left: secs,
    nextAt: performance.now() + 1000, held: false, committing: false};
  showCount();
  chime("tick");
  clearInterval(cdClock);
  cdClock = setInterval(() => countClock(performance.now()), 200);
  if (v.spoken) {
    // Spoken over the count; the phase stays "countdown".
    speak({text: v.spoken}, {caption: v.spoken}).catch(() => {});
  }
}

function showCount(held) {
  if (!cd) return;
  setPhase("countdown", {count: cd.left, label: `${cd.verb} IN ${cd.left} · SAY STOP`, held: !!held});
}

// Driven by the 200 ms timer and by every mic block (a background tab throttles timers, not audio).
function countClock(now) {
  const c = cd;
  if (!c || c.held || c.committing || now < c.nextAt) return;
  c.left -= 1;
  c.nextAt = now + 1000;
  if (c.left > 0) { chime("tick"); showCount(); return; }
  commitCountdown(c);
}

function holdCountdown() {
  if (!cd || cd.committing) return;
  cd.held = true;
  showCount(true);
}

function releaseHold() {
  if (!cd || !cd.held) return;
  cd.held = false;
  cd.left = Math.max(cd.left, COUNT_MIN_LEFT);
  cd.nextAt = performance.now() + 1000;
  showCount();
}

function dropCountdown() {
  clearInterval(cdClock);
  cdClock = 0;
  cd = null;
}

function onCountWords(text, meta) {
  if (isStop(text)) {
    const arm = cd && cd.arm;
    heard(text, meta);
    stopLocally();
    enqueueTurn(meta.tid, text, {stop: true, arm});   // the server notes the stop (and disarms)
    return;
  }
  heard(text, Object.assign({ignored: true}, meta));
  say("you", text, "IGNORED · ONLY “STOP” COUNTS NOW", "amber");
  releaseHold();
}

// The page stops at once; the server is told by the stop turn or /voice/disarm.
function stopLocally() {
  dropCountdown();
  stopSpeech();
  chime("stop");
  focus(STOPPED_CARD);
  setPhase("done", {label: "STOPPED"});
}

async function commitCountdown(c) {
  if (c.committing) return;
  c.committing = true;
  clearInterval(cdClock);
  setPhase("countdown", {count: 0, label: `${c.verb} · SAY STOP`});
  const t0 = performance.now();
  let r = null, err = null;
  for (;;) {
    if (cd !== c || !isVoice()) return;              // stopped meanwhile: nothing is sent
    try { r = await V.api("/voice/commit", {session_id: V.sessionId, arm_id: c.arm}); break; }
    catch (e) {
      err = e;
      if (e && e.status === 409 && /held/i.test(e.message || "") && performance.now() - t0 < HELD_MAX_MS) { await sleep(HELD_RETRY_MS); continue; }
      break;
    }
  }
  if (cd === c) dropCountdown();
  else if (!r) return;                             // stopped meanwhile and nothing was committed
  if (!r) {
    console.warn("Vaani: /voice/commit refused", err);
    stopSpeech();
    chime("stop");
    focus(STOPPED_CARD);
    const text = err && err.status === 410 && /expired/i.test(err.message || "") ? "That took too long, so nothing was sent." : "Stopped. Nothing was sent.";
    say("vaani", text, "■ NOT SENT", "amber");
    const out = await speak({text}, {phase: "done", label: "STOPPED", caption: text});
    if (out !== "stopped") afterSpeech("listening");
    return;
  }
  publish(r, "/voice/commit", {arm_id: c.arm});
  await onResult(r, {path: "/voice/commit"});
}

function publish(r, path, payload) {
  try { if (typeof V.renderTurn === "function") V.renderTurn(r); if (V.scrollDown) V.scrollDown(); }
  catch (e) { console.warn("Vaani: render failed", e); }
  V.emit("turn:result", {r, path, payload, via: "voice"});
}

// Esc, a tap on the core, End voice session or a lost stop turn: the server disarms, the page shows it.
async function disarm(reason, opts = {}) {
  const had = !!cd;
  if (had) stopLocally();
  if (!V.sessionId) return had;
  let r = null;
  try { r = await V.api("/voice/disarm", {session_id: V.sessionId, reason}); }
  catch (e) { console.warn("Vaani: /voice/disarm failed", e); }
  if (r && r.voice) {
    publish(r, "/voice/disarm", {reason});
    await onResult(r, {stop: true, quiet: !!opts.quiet, after: opts.after});
  } else if (had) {
    say("vaani", "Stopped. Nothing was sent.", "■ NOT SENT", "amber");
    if (!opts.quiet) afterSpeech(opts.after || "listening");
  }
  return had;
}

// ---------------------------------------------------------------- heads-up before a meeting
function startHeadsUp() {
  if (headsTimer) return;
  headsTimer = setInterval(checkHeadsUp, HEADSUP_POLL_MS);
  setTimeout(checkHeadsUp, 3000);
}
function stopHeadsUp() { clearInterval(headsTimer); headsTimer = 0; }
const idle = () => (phase === "standby" || phase === "listening") && !cd && !vturn && !playing && !cap && !V.busy;

function hhmm(iso) {
  const d = new Date(iso);
  const tz = V.me && V.me.tz ? {timeZone: V.me.tz} : {};
  try { return d.toLocaleTimeString("en-GB", Object.assign({hour: "2-digit", minute: "2-digit"}, tz)); }
  catch (_) { return d.toLocaleTimeString(); }
}

async function checkHeadsUp() {
  if (!isVoice() || !unlocked || !stream || !idle()) return;
  let j;
  try { j = await V.api("/meetings/today"); } catch (_) { return; }
  const now = Date.now();
  for (const ev of (j && j.events) || []) {
    if (!ev || ev.all_day || !ev.start) continue;
    const key = `${ev.id || ev.title}@${ev.start}`;
    if (announced.has(key)) continue;
    const t = Date.parse(ev.start);
    const mins = (t - now) / 60000;
    if (isNaN(t) || mins < HEADSUP_MIN - HEADSUP_SLACK_MIN || mins > HEADSUP_MIN + HEADSUP_SLACK_MIN) continue;
    if (!isVoice() || !idle()) return;
    announced.add(key);
    try { sessionStorage.setItem("vaani.headsup", JSON.stringify([...announced].slice(-50))); } catch (_) { /* memory only */ }
    await headsUp(ev);
    return;
  }
}

async function headsUp(ev) {
  const title = String(ev.title || "").replace(/\s+/g, " ").trim() || "Your next meeting";
  const text = `Heads-up: ${title} starts in ten minutes.`;
  chime("heads_up");
  focus({kind: "meeting", label: "HEADS-UP · VAANI SPOKE FIRST", title: `${title} · ${hhmm(ev.start)}`,
    sub: ["Starts in 10 minutes", ev.where].filter(Boolean).join(" · ")});
  say("vaani", text, "HEADS-UP · VAANI SPOKE FIRST");
  const out = await speak({text}, {phase: "speaking", caption: text});
  if (out !== "stopped") afterSpeech("listening");
}

// ---------------------------------------------------------------- public actions
function firstName() {
  const m = V.me || {};
  return String(m.first_name || m.given_name || m.name || "").trim().split(/\s+/)[0] || "";
}
function greetWord() { const h = new Date().getHours(); return h < 12 ? "Good morning" : h < 17 ? "Good afternoon" : "Good evening"; }

// The one tap per visit: must be called from a click handler (audio + mic unlock happen inside it).
function wake() {
  audio();                                   // created / resumed inside the click
  if (!isVoice()) return Promise.resolve(false);
  if (waking) return waking;
  if (unlocked && stream) { tapCore(); return Promise.resolve(true); }
  const mic = openMic();                     // getUserMedia starts inside the click
  chime("wake");
  waking = (async () => {
    const ok = await mic;
    if (!ok) { chime("error"); setPhase("asleep"); return false; }
    unlocked = true;
    startHeadsUp();
    await waitIdle(10000);                   // the first chat may still be starting
    let text = "";
    try { const j = await V.api(`/voice/greeting?${sidQ()}`); text = String((j && j.text) || "").trim(); }
    catch (e) { console.warn("Vaani: /voice/greeting failed", e); }
    if (!text) { const n = firstName(); text = `${greetWord()}${n ? `, ${n}` : ""}. Where shall we start?`; }
    if (!isVoice()) return true;
    say("vaani", text);
    const out = await speak({text}, {phase: "speaking", caption: text});
    if (out !== "stopped") afterSpeech("listening");
    return true;
  })().finally(() => { waking = null; });
  return waking;
}

function tapCore() {
  audio();
  if (!isVoice()) return;
  if (!unlocked || !stream) { wake(); return; }
  if (cd) { disarm("tap"); return; }
  if (playing || cueP || speakAbort) { stopSpeech(); stopCue(); setPhase("listening"); return; }
  if (phase === "standby" || phase === "done" || phase === "asleep") { chime("listen"); caption(""); heard(""); setPhase("listening"); return; }
  if (phase === "listening" || phase === "approval") openWindow(phase === "approval" ? APPROVAL_MS : FOLLOWUP_MS);
}

// Stops whatever Vaani is saying (and a countdown, which then sends nothing).
function stop(reason) {
  if (cd) { disarm(reason || "stop"); return; }
  const was = !!(playing || cueP || speakAbort);
  stopSpeech(); stopCue();
  if (was && isVoice() && unlocked) setPhase("listening");
}

async function endSession() {
  stopSpeech(); stopCue();
  abortCapture();
  if (cd) await disarm("end_session", {quiet: true, after: "standby"});
  if (!isVoice()) return;
  setPhase(unlocked && stream ? "standby" : "asleep");
  caption(""); heard("");
}

function setMuted(b) {
  muted = !!b;
  try { localStorage.setItem("vaani.voice.muted", muted ? "1" : "0"); } catch (_) { /* this page only */ }
  if (muted) { stopSpeech("muted"); stopCue(); }
  V.emit("voice:mute", {muted});
}

// ---------------------------------------------------------------- mode switches
function toChat() {
  stopSpeech(); stopCue();
  dropCountdown();                           // the server disarms on /voice/mode chat
  closeWindow();
  releaseMic();
  stopHeadsUp();
  setPhase(unlocked ? "standby" : "asleep");
  caption(""); heard("");
}

async function toVoice() {
  if (!unlocked) { setPhase("asleep"); return; }
  const ok = await openMic();
  if (!ok || !isVoice()) { setPhase("asleep"); return; }
  startHeadsUp();
  meter();
  // The server's own copy of the waiting card (a tap in Chat may have changed it): focus card + read-back.
  let d = pendingDraft;
  try {
    const w = await V.api(`/voice/card?${sidQ()}`);
    d = w && w.draft_id ? w : null;
  } catch (_) { /* keep the card the page last saw */ }
  if (!isVoice()) return;
  if (d && d.draft_id) {
    if (d.focus) focus(d.focus);
    const out = await speak({readback_draft_id: d.draft_id}, {phase: "approval", label: "READING IT BACK", caption: d.spoken || ""});
    if (out === "stopped") return;
    afterSpeech(out === "refused" || out === "failed" ? "listening" : "approval");
    return;
  }
  setPhase("listening");
}

function onKey(e) {
  if (e.key !== "Escape" || e.isComposing || e.defaultPrevented || !isVoice()) return;
  if (document.querySelector("dialog[open]")) return;
  if (cd) disarm("esc");
  else if (playing || cueP || speakAbort) { stopSpeech(); stopCue(); setPhase("listening"); }
  else return;
  e.preventDefault();
}

// ---------------------------------------------------------------- wiring
V.hands = {
  wake, tapCore, stop, endSession, setMuted,
  get muted() { return muted; },
  get phase() { return phase; },
  _pure: {Vad, resample, encodeWav, rmsOf, isStop, isFast, norm, consts: {WAKE_MAX_MS, LISTEN_MAX_MS, SILENCE_MS, ONSET_MS}},
};

V.on("turn:result", e => { if (e && e.r) pendingDraft = e.r.pending_draft || null; });
V.on("turn:step", onStep);
V.on("chat:new", () => {
  pendingDraft = null;
  if (cd) dropCountdown();
  if (playing || cueP) { stopSpeech(); stopCue(); if (isVoice() && unlocked) setPhase("listening"); }
  focus(null);
});
V.on("mode:change", e => {
  if (!e) return;
  if (e.mode === "chat") toChat();
  else if (e.mode === "voice") toVoice().catch(err => console.warn("Vaani: voice mode failed", err));
});

V.on("ready", () => {
  window.addEventListener("keydown", onKey);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) meter(); });
  setPhase("asleep");
  V.emit("voice:mute", {muted});
  V.api("/voice/status").then(s => {
    wakeReady = !s || s.wake !== "off";
    if (phase === "standby") setPhase("standby");
  }).catch(() => {});
});
})();
