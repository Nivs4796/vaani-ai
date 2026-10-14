/* voice.js: talk to Vaani and hear it answer, all on this Mac.
 *
 * One upload per utterance to POST /voice/stt (transcribed locally); spoken replies come from
 * POST /voice/tts as WAV and play inside the page through Web Audio, so the browser's echo
 * canceller hears them and the orb can show a playback level. A spoken request is an ordinary
 * chat turn (via "voice"): the transcript appears as your message, cards appear as usual, and
 * voice never presses a card button. States: idle -> listening -> transcribing -> thinking ->
 * speaking -> idle, announced as "voice:state". Contract: .build/CONTRACT-v2.md (VOICE-CLIENT).
 *
 * Voice mode (html.mode-voice, contract v3) belongs to handsfree.js: there this file stays out of
 * the way (no auto-listen, no spoken turn results, no keys) and only lends the page's one
 * AudioContext (Vaani.audio) and its decode / level helpers (Vaani.voiceKit).
 */
"use strict";
(() => {
const V = window.Vaani;
const SVG = "http://www.w3.org/2000/svg";
const MAX_MS = 15000, SILENCE_MS = 1200, FLOOR_MS = 300, POLL_MS = 3000;
const TITLE = "Talk to Vaani (Ctrl+Shift+Space)";
const WARMING = "Voice is warming up, try again in a moment";
const LABEL = {listening: "Listening…", transcribing: "Transcribing…", thinking: "Vaani is thinking…", speaking: "Vaani is speaking…"};
const MIME = ["audio/webm;codecs=opus", "audio/mp4", "audio/ogg;codecs=opus"].find(t =>
  window.MediaRecorder && MediaRecorder.isTypeSupported && MediaRecorder.isTypeSupported(t)) || "";

// ---------------------------------------------------------------- preferences (localStorage may be blocked)
const KEY = {speak: "vaani.voice.speak", handsFree: "vaani.voice.handsfree", voice: "vaani.voice.name"};
function load(k, dflt) { try { const v = localStorage.getItem(k); return v == null ? dflt : v; } catch (_) { return dflt; } }
function save(k, v) { try { localStorage.setItem(k, String(v)); } catch (_) { /* kept for this page only */ } }
const prefs = {speak: load(KEY.speak, "1") !== "0", handsFree: load(KEY.handsFree, "0") === "1", voice: load(KEY.voice, "")};

// ---------------------------------------------------------------- state
let status = null, enabled = false, state = "idle";
let mic = null, dock = null, label = null, stopBtn = null, pollTimer = 0, flashTimer = 0;
let ctx = null, rec = null, listenSeq = 0, starting = false;
let speakSeq = 0, speakAbort = null, playing = null, pendingPlays = 0;
let handsFreePaused = false, skipNext = false, lastBrief = null, briefObs = null;
let raf = 0, level = 0;

const csrf = () => (V.me && V.me.csrf) || "";
const sttReady = () => !!status && status.stt === "ready";
const voiceMode = () => document.documentElement.classList.contains("mode-voice");
const handsFreeOn = () => prefs.handsFree && !handsFreePaused && sttReady() && !voiceMode();
const ttsActive = () => pendingPlays > 0 || !!playing;

function setState(s) {
  if (s === state) return;
  state = s;
  clearTimeout(flashTimer);
  paint();
  V.emit("voice:state", {state: s});
}

// One AudioContext for the page, created (or resumed) inside the click that first needs it.
function audioCtx() {
  const AC = window.AudioContext || window.webkitAudioContext;
  if (!AC) return null;
  try {
    if (!ctx) ctx = new AC();
    if (ctx.state === "suspended") Promise.resolve(ctx.resume()).catch(() => {});
  } catch (_) { return null; }
  return ctx;
}

function notice(text, tone) {
  if (typeof V.toast === "function") {
    try { V.toast(text, tone ? {tone} : {}); return; } catch (_) { /* fall through to the pill */ }
  }
  if (!dock || state !== "idle") return;
  dock.dataset.state = "note"; dock.classList.remove("is-idle"); label.textContent = text;
  clearTimeout(flashTimer);
  flashTimer = setTimeout(paint, 3500);
}

// ---------------------------------------------------------------- icons (built as DOM, never innerHTML)
function icon(paths, size) {
  const s = document.createElementNS(SVG, "svg");
  s.setAttribute("viewBox", "0 0 20 20"); s.setAttribute("width", size); s.setAttribute("height", size);
  s.setAttribute("aria-hidden", "true"); s.setAttribute("focusable", "false");
  for (const [tag, attrs] of paths) {
    const p = document.createElementNS(SVG, tag);
    for (const [k, v] of Object.entries(attrs)) p.setAttribute(k, v);
    p.setAttribute("fill", "none"); p.setAttribute("stroke", "currentColor");
    p.setAttribute("stroke-width", "1.6"); p.setAttribute("stroke-linecap", "round"); p.setAttribute("stroke-linejoin", "round");
    s.append(p);
  }
  return s;
}
const micIcon = size => icon([["rect", {x: "7", y: "2.5", width: "6", height: "10", rx: "3"}],
  ["path", {d: "M4.5 9.5a5.5 5.5 0 0 0 11 0M10 15v2.5"}]], size);
const speakerIcon = size => icon([["path", {d: "M3.5 8v4h3l4 3.5v-11l-4 3.5z"}],
  ["path", {d: "M13.5 7.5a3.5 3.5 0 0 1 0 5M15.5 5a7 7 0 0 1 0 10"}]], size);

// ---------------------------------------------------------------- composer UI
function buildUi() {
  mic = V.h("button", {type: "button", class: "vv-mic", "aria-label": "Start voice", title: TITLE}, micIcon(18));
  mic.addEventListener("click", toggle);
  const slot = document.getElementById("micSlot"), send = document.getElementById("send");
  if (slot) slot.append(mic);
  else if (send && send.parentNode) send.parentNode.insertBefore(mic, send);
  else return false;

  // The status pill sits just above the composer. It is only visually hidden when idle, so the
  // live region is always in the accessibility tree and every change is announced.
  label = V.h("span", {class: "vv-label"});
  stopBtn = V.h("button", {type: "button", class: "vv-stop", hidden: true}, "Stop");
  stopBtn.addEventListener("click", () => { handsFreePaused = true; stopSpeaking(); });
  dock = V.h("div", {class: "vv-dock is-idle", "data-state": "idle"},
    V.h("div", {class: "vv-status", role: "status", "aria-live": "polite"}, V.h("span", {class: "vv-orb", "aria-hidden": "true"}), label),
    stopBtn);
  const form = document.getElementById("f");
  if (form && form.parentNode) form.parentNode.insertBefore(dock, form);
  else { dock.classList.add("vv-float"); document.body.append(dock); }
  return true;
}

function paint() {
  if (!mic || !dock) return;
  const stt = status ? status.stt : "off";
  mic.classList.toggle("is-warming", stt === "loading");
  mic.classList.toggle("is-error", stt === "error");
  mic.classList.toggle("is-listening", state === "listening");
  mic.classList.toggle("is-busy", state === "transcribing" || state === "thinking");
  mic.classList.toggle("is-speaking", state === "speaking");
  mic.setAttribute("aria-label", stt === "loading" ? "Voice is warming up" : stt === "error" ? "Voice is unavailable"
    : state === "listening" ? "Stop and send" : state === "speaking" ? "Stop Vaani and talk" : "Start voice");
  mic.title = stt === "loading" ? "Voice is warming up…" : state === "listening" ? "Stop and send (Esc cancels)" : TITLE;
  const text = LABEL[state] || "";
  dock.dataset.state = state;
  dock.classList.toggle("is-idle", !text);
  if (label.textContent !== text) label.textContent = text;
  stopBtn.hidden = state !== "speaking";
}

function paintLevel(v) {
  const s = v.toFixed(3);
  if (dock) dock.style.setProperty("--level", s);
  if (mic) mic.style.setProperty("--level", s);
}

// ---------------------------------------------------------------- status (warming up while the model loads)
async function fetchStatus() { try { return await V.api("/voice/status"); } catch (_) { return null; } }

function setStatus(s) {
  status = s;
  clearTimeout(pollTimer);
  if (s.stt === "loading") pollTimer = setTimeout(poll, POLL_MS);
  paint();
}

async function poll() {
  const s = await fetchStatus();
  if (!s) { pollTimer = setTimeout(poll, POLL_MS); return; }
  setStatus(s);
}

// ---------------------------------------------------------------- mic tap / keyboard
function toggle() {
  const stt = status ? status.stt : "off";
  if (stt === "loading") { notice(WARMING, "warn"); return; }
  if (stt === "error") { notice(`Voice is unavailable: ${status.error || "the speech model did not load"}`, "risk"); return; }
  if (state === "listening") { stopListening(true); return; }
  if (state === "transcribing" || starting) return;
  if (state === "speaking" || ttsActive()) stopSpeaking();              // barge-in: silence first, then listen
  else if (V.busy || !V.sessionId) { notice("Vaani is busy. Try again in a moment.", "warn"); return; }
  handsFreePaused = false;
  startListening();
}

function inTextField(t) {
  if (!t || t.id === "box") return false;    // the composer always has focus; the chord types nothing there
  if (t.isContentEditable || t.tagName === "TEXTAREA" || t.tagName === "SELECT") return true;
  return t.tagName === "INPUT" && !/^(button|checkbox|radio|submit|reset|range|color|file)$/i.test(t.type || "");
}

// Capture phase, so an Esc meant for voice does not also close a drawer underneath.
function onKey(e) {
  if (!enabled || e.isComposing) return;
  if (voiceMode() && !rec && !starting) return;     // voice mode: handsfree.js owns Esc and the core
  if (e.key === "Escape") {
    let used = true;
    if (rec || starting || state === "transcribing") cancelListening();
    else if (state === "speaking" || ttsActive()) stopSpeaking();
    else if (state === "thinking") { skipNext = true; setState("idle"); }   // the turn runs on, but is not read out
    else used = false;
    if (used) { handsFreePaused = true; e.preventDefault(); e.stopPropagation(); }
    return;
  }
  if ((e.code === "Space" || e.key === " ") && e.ctrlKey && e.shiftKey && !e.altKey && !e.metaKey) {
    if (inTextField(e.target)) return;
    e.preventDefault();
    toggle();
  }
}

// ---------------------------------------------------------------- listening
async function startListening() {
  if (rec || starting) return;
  const ac = audioCtx();     // inside the tap: this is what lets audio start later
  const md = navigator.mediaDevices;
  if (!md || !md.getUserMedia || !window.MediaRecorder) { notice("This browser cannot record audio on this page", "warn"); return; }
  const my = ++listenSeq;
  starting = true;
  let stream;
  try {
    stream = await md.getUserMedia({audio: {echoCancellation: true, noiseSuppression: true, autoGainControl: true}});
  } catch (e) {
    starting = false;
    if (my !== listenSeq) return;
    handsFreePaused = true;
    const name = e && e.name;
    notice(name === "NotAllowedError" || name === "SecurityError" ? "Microphone blocked: allow it in the address bar"
      : name === "NotFoundError" ? "No microphone found" : "Could not open the microphone", "risk");
    return;
  }
  starting = false;
  if (my !== listenSeq) { stream.getTracks().forEach(t => t.stop()); return; }
  let recorder;
  try { recorder = MIME ? new MediaRecorder(stream, {mimeType: MIME}) : new MediaRecorder(stream); }
  catch (_) { stream.getTracks().forEach(t => t.stop()); notice("This browser cannot record audio on this page", "warn"); return; }

  const r = {my, stream, recorder, chunks: [], t0: performance.now(), floorSum: 0, floorN: 0, thresh: 0,
    run: 0, heard: false, lastLoud: 0, send: false, done: false};
  if (ac) {
    try {
      // Mic -> analyser -> silent gain -> destination: the analyser is always pulled, nothing is heard.
      r.src = ac.createMediaStreamSource(stream);
      r.an = ac.createAnalyser(); r.an.fftSize = 1024; r.buf = new Float32Array(r.an.fftSize);
      r.sink = ac.createGain(); r.sink.gain.value = 0;
      r.src.connect(r.an); r.an.connect(r.sink); r.sink.connect(ac.destination);
    } catch (_) { r.an = null; }
  }
  recorder.ondataavailable = e => { if (e.data && e.data.size) r.chunks.push(e.data); };
  recorder.onstop = () => { if (rec === r) stopListening(r.heard); else finish(r); };
  recorder.onerror = () => { if (rec === r) { stopListening(false); notice("Recording stopped unexpectedly", "warn"); } };
  rec = r;
  r.maxTimer = setTimeout(() => { if (rec === r) stopListening(true); }, MAX_MS);
  try { recorder.start(); } catch (_) { stopListening(false); notice("Could not start recording", "risk"); return; }
  setState("listening");
  meter();
}

// Ends the utterance: send uploads it, otherwise it is discarded. The mic is released either way.
function stopListening(send) {
  const r = rec;
  if (!r) return;
  rec = null; r.send = !!send;
  clearTimeout(r.maxTimer);
  try { if (r.recorder.state !== "inactive") r.recorder.stop(); else finish(r); } catch (_) { finish(r); }
  r.stream.getTracks().forEach(t => t.stop());
  for (const n of [r.src, r.an, r.sink]) { try { if (n) n.disconnect(); } catch (_) { /* already gone */ } }
  setState(send ? "transcribing" : "idle");
}

function cancelListening() {
  listenSeq++;              // a late transcript from this utterance is dropped
  starting = false;
  if (rec) stopListening(false);
  else if (state === "transcribing") setState("idle");
}

function finish(r) {
  if (r.done) return;
  r.done = true;
  if (!r.send || r.my !== listenSeq) return;
  const blob = new Blob(r.chunks, {type: r.recorder.mimeType || MIME || "audio/webm"});
  if (!blob.size) { setState("idle"); handsFreePaused = true; notice("I didn't catch that"); return; }
  transcribe(blob, r.my);
}

async function transcribe(blob, my) {
  setState("transcribing");
  let res, j = {};
  try {
    res = await fetch("/voice/stt", {method: "POST", body: blob, credentials: "same-origin",
      headers: {"Content-Type": blob.type.split(";")[0] || "audio/webm", "X-CSRF-Token": csrf()}});
    j = await res.json().catch(() => ({}));
  } catch (_) {
    if (my === listenSeq) { setState("idle"); notice("Voice could not reach Vaani on this Mac", "risk"); }
    return;
  }
  if (my !== listenSeq) return;                       // cancelled with Esc meanwhile
  if (res.status === 401) { location.href = "/login"; return; }
  if (res.status === 503) { setState("idle"); notice(WARMING, "warn"); setStatus(Object.assign({}, status, {stt: "loading"})); return; }
  if (!res.ok) {
    setState("idle");
    notice(res.status === 413 ? "That was too long for one go. Try a shorter request." : `Voice failed: ${j.error || res.status}`, "risk");
    return;
  }
  const text = String(j.text || "").trim();
  if (!text || j.empty) { setState("idle"); handsFreePaused = true; notice("I didn't catch that"); return; }
  await submit(text, my);
}

function waitIdle(ms) {
  const t0 = Date.now();
  return new Promise(done => {
    (function check() { if ((!V.busy && V.sessionId) || Date.now() - t0 > ms) done(); else setTimeout(check, 200); })();
  });
}

// The transcript goes through the normal chat path, exactly as if it had been typed.
async function submit(text, my) {
  if (V.busy || !V.sessionId) await waitIdle(30000);
  if (my !== listenSeq) return;                       // cancelled with Esc while waiting
  if (V.busy || !V.sessionId) {
    setState("idle");
    const box = document.getElementById("box");
    if (box) box.value = text;
    notice("Vaani is still busy. Your words are in the message box.", "warn");
    return;
  }
  setState("thinking");
  skipNext = false;
  await V.submit(text, {via: "voice"});
  if (state === "thinking" && !ttsActive()) setState("idle");
}

// ---------------------------------------------------------------- level meter and silence detection
function rms(an, buf) {
  if (an.getFloatTimeDomainData) an.getFloatTimeDomainData(buf);
  else { const b = new Uint8Array(buf.length); an.getByteTimeDomainData(b); for (let i = 0; i < b.length; i++) buf[i] = (b[i] - 128) / 128; }
  let s = 0;
  for (let i = 0; i < buf.length; i++) s += buf[i] * buf[i];
  return Math.sqrt(s / buf.length);
}

function meter() { if (!raf) raf = requestAnimationFrame(tick); }

function tick(now) {
  raf = 0;
  let target = 0;
  const r = rec;
  if (r && r.an) {
    const v = rms(r.an, r.buf);
    if (now - r.t0 < FLOOR_MS) { r.floorSum += v; r.floorN++; }
    else {
      if (!r.thresh) {
        // Noise floor from the first 300 ms (capped, in case speech started at once) plus a margin.
        const floor = Math.min(r.floorN ? r.floorSum / r.floorN : 0.01, 0.04);
        r.floor = floor; r.thresh = Math.max(0.012, floor * 2.5 + 0.006);
      }
      if (v > r.thresh) {
        if (++r.run >= 3) r.heard = true;        // ~50 ms above the floor: speech, not a click
        if (r.heard) r.lastLoud = now;
      } else {
        r.run = 0;
        if (r.heard && now - r.lastLoud > SILENCE_MS) { stopListening(true); }
      }
    }
    target = Math.min(1, Math.max(0, v - (r.floor || 0)) / 0.12);
  } else if (playing) {
    target = playing.an ? Math.min(1, rms(playing.an, playing.buf) / 0.18) : 0.35;
  }
  level += (target - level) * (target > level ? 0.5 : 0.12);    // quick attack, slow release
  if (!rec && !playing && level < 0.01) level = 0;
  paintLevel(level);
  if (rec || playing || level > 0) raf = requestAnimationFrame(tick);
}

// ---------------------------------------------------------------- speaking
function decode(ac, buf) {
  return new Promise((ok, no) => {
    const p = ac.decodeAudioData(buf, ok, no);
    if (p && p.then) p.then(ok, no);
  });
}

// Stops playback at once: the source stops, a pending /voice/tts fetch is aborted, and any audio
// that still arrives for an older request is dropped (speakSeq moved on).
function stopSpeaking(quiet) {
  speakSeq++;
  if (speakAbort) { try { speakAbort.abort(); } catch (_) { /* already settled */ } speakAbort = null; }
  const p = playing;
  playing = null;
  if (p) {
    if (p.synth) { try { window.speechSynthesis.cancel(); } catch (_) { /* nothing queued */ } }
    else {
      p.src.onended = null;
      try { p.src.stop(); } catch (_) { /* not started */ }
      try { p.src.disconnect(); p.an.disconnect(); } catch (_) { /* already gone */ }
    }
    p.done("stopped");
  }
  if (!quiet && (state === "speaking" || (state === "thinking" && !V.busy))) setState("idle");
}

// Plays one /voice/tts request. Resolves "done" (played to the end), "silent" (nothing to say),
// "stopped" (interrupted or replaced) or "failed" (nothing could be spoken).
async function play(body) {
  stopSpeaking(true);
  const my = ++speakSeq;
  pendingPlays++;
  let out;
  try { out = await playInner(body, my); }
  finally { pendingPlays--; }
  if (my === speakSeq && !rec && (state === "speaking" || (state === "thinking" && !V.busy))) setState("idle");
  return out;
}

async function playInner(body, my) {
  const ac = audioCtx();       // created inside the click when a button called us
  const ctl = new AbortController();
  speakAbort = ctl;
  if (prefs.voice) body = Object.assign({voice: prefs.voice}, body);
  let buf;
  try {
    const res = await fetch("/voice/tts", {method: "POST", credentials: "same-origin", signal: ctl.signal,
      headers: {"Content-Type": "application/json", "X-CSRF-Token": csrf()}, body: JSON.stringify(body)});
    if (my !== speakSeq) return "stopped";
    if (res.status === 401) { location.href = "/login"; return "failed"; }
    if (res.status === 204) return "silent";
    if (!res.ok) throw new Error(`/voice/tts failed (${res.status})`);
    buf = await res.arrayBuffer();
  } catch (e) {
    if (my !== speakSeq || (e && e.name === "AbortError")) return "stopped";
    return synth(localText(body), my);
  } finally { if (speakAbort === ctl) speakAbort = null; }
  if (my !== speakSeq) return "stopped";
  if (!ac || !buf || !buf.byteLength) return buf && buf.byteLength ? synth(localText(body), my) : "silent";
  let audio;
  try { audio = await decode(ac, buf); } catch (_) { return my === speakSeq ? synth(localText(body), my) : "stopped"; }
  if (ac.state !== "running") {
    try { await Promise.race([Promise.resolve(ac.resume()).catch(() => {}), new Promise(ok => setTimeout(ok, 400))]); }
    catch (_) { /* still locked: handled below */ }
  }
  if (my !== speakSeq) return "stopped";
  if (ac.state !== "running") return synth(localText(body), my);   // the browser kept audio locked
  return new Promise(resolve => {
    const src = ac.createBufferSource(), an = ac.createAnalyser();
    an.fftSize = 1024;
    src.buffer = audio;
    src.connect(an); an.connect(ac.destination);
    const p = {src, an, buf: new Float32Array(an.fftSize), done: resolve};
    playing = p;
    src.onended = () => {
      if (playing !== p) return;
      playing = null;
      try { src.disconnect(); an.disconnect(); } catch (_) { /* already gone */ }
      resolve("done");
    };
    setState("speaking");
    meter();
    src.start();
  });
}

// Fallback when /voice/tts fails: the browser's own voices, on-device ones only (never network
// voices), one sentence per utterance.
function localVoices() {
  const ss = window.speechSynthesis;
  const pick = () => ss.getVoices().filter(v => v.localService === true);
  const now = pick();
  if (now.length) return Promise.resolve(now);
  return new Promise(done => {
    const t = setTimeout(() => done(pick()), 1000);
    if (ss.addEventListener) ss.addEventListener("voiceschanged", () => { clearTimeout(t); done(pick()); }, {once: true});
  });
}

const sentences = text => (String(text).match(/[^.!?]+(?:[.!?]+|$)/g) || []).map(s => s.trim()).filter(Boolean);

async function synth(text, my) {
  const ss = window.speechSynthesis;
  if (!ss || !window.SpeechSynthesisUtterance || !text) return "failed";
  const voices = await localVoices();
  if (my !== speakSeq) return "stopped";
  if (!voices.length) return "failed";
  const want = prefs.voice || (status && status.voice) || "";
  const en = v => /^en([-_]|$)/i.test(v.lang || "");
  const voice = voices.find(v => v.name === want) || voices.find(v => v.default && en(v)) || voices.find(en) || voices[0];
  const parts = sentences(text);
  return new Promise(resolve => {
    const p = {synth: true, done: resolve};
    let i = 0;
    playing = p;
    const next = () => {
      if (playing !== p) return;                 // stopped: stopSpeaking already resolved it
      if (i >= parts.length) { playing = null; resolve("done"); return; }
      const u = new SpeechSynthesisUtterance(parts[i++]);
      u.voice = voice; u.lang = voice.lang;
      u.onend = next; u.onerror = next;
      ss.speak(u);
    };
    setState("speaking");
    meter();
    next();
  });
}

// What the fallback says for a turn: at most two plain sentences of the reply, no links, ids or
// addresses, nothing claiming "sent / done / created" without an ok receipt, plus the card prompt.
function localText(body) {
  if (body.text) return body.text;
  const r = body.result || {};
  const rc = r.receipt;
  const verified = rc && (typeof rc.verified === "number" ? (rc.count ? rc.verified >= rc.count : rc.verified > 0) : rc.verified === true);
  const okReceipt = !!rc && verified && !/ERROR|FAIL|NOT/i.test(rc.status || "");
  const plain = String(r.reply || "")
    .replace(/\[([^\]\n]+)\]\([^)\s]+\)/g, "$1")
    .replace(/https?:\/\/\S+/g, "")
    .replace(/<?[\w.+-]+@[\w-]+(?:\.[\w-]+)+>?/g, "")
    .replace(/\b[0-9a-f]{12,}\b|\b[\w-]{24,}\b/gi, "")
    .replace(/^\s*(?:\d+[.)]|[-*•])\s+/gm, "")
    .replace(/[*_`#>|]/g, "")
    .replace(/\s+/g, " ").replace(/\s+([.,!?;:])/g, "$1").trim();
  const out = sentences(plain).filter(s => okReceipt || !/\b(sent|done|created)\b/i.test(s)).slice(0, 2);
  const d = r.pending_draft;
  if (d) out.push(`I've drafted it. Tap ${(d.card && d.card.confirm_label) || "Confirm"} on the card to go ahead.`);
  return out.join(" ");
}

// Public: read any text aloud (the brief's Listen button, Test voice, other feature files).
function speak(text) {
  const t = String(text || "").trim();
  if (!t) return Promise.resolve("silent");
  if (rec || starting) cancelListening();
  return play({text: t});
}

// A turn's reply, built into speech by the server (voice.spoken_text). Hands-free listens again after.
async function speakTurn(r) {
  const out = await play({result: r});
  if ((out === "done" || out === "silent") && handsFreeOn() && state === "idle" && !V.busy && !rec) startListening();
}

// ---------------------------------------------------------------- brief "Listen" pill
function listenButton(getText, what) {
  const word = V.h("span", {}, "Listen");
  const b = V.h("button", {type: "button", class: "vv-listen", "aria-label": `Listen to ${what}`}, speakerIcon(14), word);
  const reset = () => { b.classList.remove("is-playing"); word.textContent = "Listen"; b.setAttribute("aria-label", `Listen to ${what}`); };
  b.addEventListener("click", async () => {
    if (b.classList.contains("is-playing")) { stopSpeaking(); return; }
    const text = (getText() || "").trim();
    if (!text) return;
    b.classList.add("is-playing"); word.textContent = "Stop"; b.setAttribute("aria-label", "Stop reading");
    try { await speak(text); } finally { reset(); }
  });
  return b;
}

function attachBrief(el) {
  if (briefObs) { briefObs.disconnect(); briefObs = null; }
  lastBrief = el || null;
  if (!el || !enabled) return;
  const fill = () => {
    const slot = el.querySelector(".brief-voice-slot");
    if (!slot || slot.querySelector(".vv-listen")) return;
    slot.append(listenButton(() => { const g = el.querySelector(".brief-greeting"); return g ? g.textContent : ""; }, "the greeting"));
  };
  fill();
  // The brief refreshes itself; put the pill back whenever its slot is rebuilt.
  if (window.MutationObserver) { briefObs = new MutationObserver(fill); briefObs.observe(el, {childList: true, subtree: true}); }
}

// ---------------------------------------------------------------- settings page
function describe(s) {
  if (!s) return "Not available";
  if (s.stt === "ready") return `Ready · ${s.model || "local model"} · transcribed on this Mac`;
  if (s.stt === "loading") return `Warming up${s.model ? ` ${s.model}` : ""}…`;
  if (s.stt === "error") return `Not available: ${s.error || "the speech model did not load"}`;
  return "Off";
}

// On / Off as SETTINGS' segmented control (selected = ink pill).
function onOff(name, on, onChange) {
  const seg = V.h("div", {class: "sv-seg", role: "group", "aria-label": name});
  const sync = v => { for (const b of seg.children) b.setAttribute("aria-pressed", String((b.dataset.value === "on") === v)); };
  for (const [value, text] of [["on", "On"], ["off", "Off"]]) {
    seg.append(V.h("button", {type: "button", "data-value": value, onclick: () => { sync(value === "on"); onChange(value === "on"); }}, text));
  }
  sync(on);
  return seg;
}

const settingLine = (title, sub, control) => V.h("div", {class: "sv-line"},
  V.h("div", {}, V.h("div", {class: "sv-label", text: title}), V.h("div", {class: "sv-sub", text: sub})), control);

function renderSettings(container) {
  const h = V.h;
  container.textContent = "";
  const sttLine = h("span", {class: "sv-val"}, describe(status));
  const select = h("select", {class: "vv-set-select", "aria-label": "Voice for spoken replies"});
  const fillVoices = s => {
    select.textContent = "";
    select.append(h("option", {value: ""}, `Default${s && s.voice ? ` (${s.voice})` : ""}`));
    for (const v of (s && s.voices) || []) select.append(h("option", {value: v.name}, v.lang ? `${v.name} (${v.lang})` : v.name));
    select.value = prefs.voice && [...select.options].some(o => o.value === prefs.voice) ? prefs.voice : "";
  };
  fillVoices(status);
  select.addEventListener("change", () => { prefs.voice = select.value; save(KEY.voice, select.value); });

  let testing = false;
  const testWord = h("span", {}, "Test voice");
  const test = h("button", {type: "button", class: "sv-btn vv-set-test"}, speakerIcon(14), testWord);
  test.addEventListener("click", async () => {
    if (testing) { stopSpeaking(); return; }
    testing = true; testWord.textContent = "Stop";
    try { await speak("Hello, this is Vaani. This is how I will sound when I read your replies."); }
    finally { testing = false; testWord.textContent = "Test voice"; }
  });

  container.append(
    h("section", {class: "sv-card"}, h("h2", {class: "sv-card-title", text: "Speech"}),
      h("dl", {class: "sv-rows"},
        h("div", {class: "sv-row"}, h("dt", {text: "Speech to text"}), h("dd", {}, sttLine)),
        h("div", {class: "sv-row"}, h("dt", {text: "Voice"}), h("dd", {}, h("div", {class: "vv-set-voice"}, select, test))))),
    h("section", {class: "sv-card"}, h("h2", {class: "sv-card-title", text: "Replies"}),
      settingLine("Speak replies to voice questions", "Vaani reads its answer aloud when you asked by voice.",
        onOff("Speak replies to voice questions", prefs.speak, on => { prefs.speak = on; save(KEY.speak, on ? "1" : "0"); })),
      settingLine("Hands-free", "Listen again after Vaani speaks. Esc or an empty reply ends it.",
        onOff("Hands-free", prefs.handsFree, on => { prefs.handsFree = on; handsFreePaused = false; save(KEY.handsFree, on ? "1" : "0"); })),
      h("p", {class: "sv-note"}, "Voice never presses a card button: you still tap to approve. Settings are saved in this browser.")));

  fetchStatus().then(s => {
    if (!s || !container.isConnected) return;
    setStatus(s);
    sttLine.textContent = describe(s);
    fillVoices(s);
  });
  return () => { if (testing) stopSpeaking(); };     // the page closed: stop the test audio
}

// ---------------------------------------------------------------- wiring
V.speak = speak;
V.stopSpeaking = () => stopSpeaking(false);
V.audio = audioCtx;
V.voiceKit = {decode, rms};

// Entering voice mode: the chat mic and any reply being read out stop; handsfree.js takes over.
V.on("mode:change", e => {
  if (!e || e.mode !== "voice") return;
  if (rec || starting || state === "transcribing") cancelListening();
  if (state === "speaking" || ttsActive()) stopSpeaking();
});

V.on("brief:shown", e => attachBrief(e && e.el));
V.on("chat:new", () => { if (briefObs) { briefObs.disconnect(); briefObs = null; } lastBrief = null; if (enabled) stopSpeaking(); });

V.on("turn:start", e => {
  if (!enabled || !e || e.via !== "voice") return;
  // Mark the transcript bubble as spoken (the label itself stays the plain transcript).
  const log = document.getElementById("log");
  const msgs = log ? log.querySelectorAll(".msg.user") : [];
  const m = msgs[msgs.length - 1];
  if (m && !m.dataset.via) { m.dataset.via = "voice"; m.prepend(V.h("span", {class: "vv-said"}, micIcon(12), V.h("span", {class: "vv-sr"}, "Spoken: "))); }
});

V.on("turn:result", e => {
  if (!enabled || !e || !e.r) return;
  if (voiceMode() || e.path === "/voice/turn" || e.path === "/voice/commit" || e.path === "/voice/disarm") return;  // handsfree.js speaks these
  const voiced = e.via === "voice";
  if (voiced && skipNext) { skipNext = false; return; }
  if (!((voiced && prefs.speak) || handsFreeOn())) return;
  if (rec || starting || state === "transcribing") return;      // the user is already talking again
  speakTurn(e.r);
});

V.on("turn:error", e => {
  if (e && e.via === "voice") { skipNext = false; if (state === "thinking" && !ttsActive()) setState("idle"); }
});

V.on("ready", async () => {
  const s = await fetchStatus();
  if (!s || !s.stt || s.stt === "off") return;           // no voice on this server: show nothing
  if (!buildUi()) return;
  enabled = true;
  setStatus(s);
  window.addEventListener("keydown", onKey, true);
  V.settings.register({id: "voice", title: "Voice", icon: "mic", order: 40, render: renderSettings,
    lede: "Talk to Vaani with the mic button or Ctrl+Shift+Space. Your voice is transcribed on this Mac and never saved."});
  if (lastBrief && lastBrief.isConnected) attachBrief(lastBrief);
});
})();
