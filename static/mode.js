/* mode.js: Voice / Chat mode switch (MODE). Contract: .build/CONTRACT-v3.md.
 *
 * Vaani always opens in Voice: index.html sets html.mode-voice before paint, and the mode is never
 * remembered between visits. On ready the page asks GET /voice/status; voice off, a failed speech
 * model or missing voice routes open Chat instead, with a toast.
 * Every switch, and every new chat, tells the server through POST /voice/mode: that server flag is
 * what lets a spoken word approve anything. mode:change {mode, why} is emitted after the switch and
 * that POST (to Chat it is emitted first: the safe side should never wait on the network). ENGINE
 * releases the mic (to Chat) or reads back a waiting card (to Voice) on mode:change;
 * this file only switches, and ENGINE calls Vaani.setMode("chat", {why: "voice"}) for "switch to chat".
 * The "Chat | Voice" toggle is rendered into #modeSlotChat (chat topbar) and #modeSlotHud (HUD header,
 * which hud.js may build after ready, so #hud is watched).
 */
"use strict";
(() => {
const V = window.Vaani;
const root = document.documentElement;
const POST_WAIT_MS = 1500;   // mode:change waits at most this long for POST /voice/mode
const NOTE = "Same conversation as voice mode. In chat mode, approvals are buttons again.";
const MODES = [["chat", "Chat"], ["voice", "Voice"]];

let mode = root.classList.contains("mode-chat") ? "chat" : "voice";
let chain = Promise.resolve();   // switches and server posts run one at a time, in order
let voiceOff = false;            // GET /voice/status said no; a later click on Voice checks again
let notedFor = null;             // the session whose chat thread already has the NOTE line
let lastToast = {text: "", at: 0};
let userSwitched = false;        // the user (or a spoken "switch to chat") has chosen a mode this visit

// A 404 that is "this route does not exist" (older server), not "unknown session".
const missingRoute = e => e && e.status === 404 && !/session/i.test(e.message || "");
const within = (p, ms) => Promise.race([p, new Promise(r => setTimeout(() => r({late: true}), ms))]);

function toast(text, tone) {
  const now = Date.now();
  if (text === lastToast.text && now - lastToast.at < 5000) return;   // one cause, one toast
  lastToast = {text, at: now};
  if (typeof V.toast === "function") V.toast(text, {tone: tone || "warn", ms: 6000});
}

// ---------------------------------------------------------------- the toggle

function syncToggles() {
  document.querySelectorAll(".mode-seg-btn").forEach(b => b.setAttribute("aria-pressed", String(b.dataset.mode === mode)));
}

function buildToggle() {
  const wrap = document.createElement("div");
  wrap.className = "mode-seg";
  wrap.setAttribute("role", "group");
  wrap.setAttribute("aria-label", "Mode");
  for (const [m, label] of MODES) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "mode-seg-btn";
    b.dataset.mode = m;
    b.textContent = label;
    b.setAttribute("aria-label", `${label} mode`);
    b.setAttribute("aria-pressed", String(mode === m));
    b.addEventListener("click", () => { if (mode !== m) setMode(m, {why: "user"}); });
    wrap.append(b);
  }
  // Left / Right move between the two buttons (Tab, Enter and Space work as on any button).
  wrap.addEventListener("keydown", e => {
    if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
    const bs = [...wrap.querySelectorAll(".mode-seg-btn")];
    const i = bs.indexOf(document.activeElement);
    if (i < 0) return;
    e.preventDefault();
    bs[(i + (e.key === "ArrowRight" ? 1 : bs.length - 1)) % bs.length].focus();
  });
  return wrap;
}

// Idempotent: a slot gets one toggle; a slot the HUD rebuilds gets a fresh one.
function mountToggles() {
  for (const id of ["modeSlotChat", "modeSlotHud"]) {
    const slot = document.getElementById(id);
    if (slot && !slot.querySelector(".mode-seg")) slot.append(buildToggle());
  }
}

// ---------------------------------------------------------------- switching

function applyClass(m) {
  mode = m;
  root.classList.toggle("mode-voice", m === "voice");
  root.classList.toggle("mode-chat", m === "chat");
  syncToggles();
}

// Focus follows the switch, so it never stays on a button that just went out of view.
function placeFocus(m) {
  const a = document.activeElement;
  if (m === "chat") {
    const box = document.getElementById("box");
    if (box && !box.disabled) { box.focus({preventScroll: true}); return; }
    const own = document.querySelector('#modeSlotChat .mode-seg-btn[data-mode="chat"]');
    if (own) own.focus({preventScroll: true});
    return;
  }
  const hud = document.getElementById("hud");
  if (hud && a && hud.contains(a)) return;
  const target = document.querySelector('#modeSlotHud .mode-seg-btn[data-mode="voice"]') || document.getElementById("hudWakeBtn");
  if (target) target.focus({preventScroll: true});
}

// POST /voice/mode for the current chat. -> {ok} | {missing} | {error}; no chat yet = ok (chat:new posts it).
async function postMode(m) {
  const sid = V.sessionId;
  if (!sid || typeof V.api !== "function") return {ok: true};
  try { await V.api("/voice/mode", {session_id: sid, mode: m}); return {ok: true}; }
  catch (e) {
    if (e && e.status === 401) return {ok: true};   // api() is already on its way to /login
    return missingRoute(e) ? {missing: true} : {error: (e && e.message) || "request failed"};
  }
}

// What stops Voice on this server, or null. -> {off} | {error: text} | null.
async function voiceProblem() {
  if (typeof V.api !== "function") return null;
  try {
    const s = await V.api("/voice/status");
    if (s && s.stt === "off") return {off: true};
    if (s && s.stt === "error") return {error: s.error || "the speech model failed to load"};
    return null;
  } catch (e) {
    if (e && e.status === 401) return null;
    return missingRoute(e) ? {off: true, missing: true} : {error: (e && e.message) || "voice status failed"};
  }
}

// ctx: "start" (nobody has picked a mode yet), "server" (Vaani moves to Chat by itself), "click" (Voice was refused).
function problemText(p, ctx) {
  const tail = {start: ", so Vaani opened in Chat.", server: ", so Vaani switched to Chat.", click: "."}[ctx] || ".";
  return p.off ? `Voice is off on this server${tail}` : `Voice could not start (${p.error})${tail}`;
}

// One quiet line in the chat thread the first time this chat comes back from Voice (as in the mock-up).
function noteInThread() {
  const sid = V.sessionId;
  if (!sid || notedFor === sid || typeof V.add !== "function" || typeof V.h !== "function") return;
  notedFor = sid;
  V.add(V.h("p", {class: "mode-note", role: "note", text: NOTE}));
}

async function switchTo(next, why, opts = {}) {
  if (next === mode) { syncToggles(); return mode; }
  if (next === "voice" && voiceOff) {
    const p = await voiceProblem();
    if (p) { toast(problemText(p, "click")); syncToggles(); return mode; }
    voiceOff = false;
  }
  const from = mode;
  applyClass(next);
  placeFocus(next);
  if (next === "chat") {
    // The thread was display:none in Voice, which loses its scroll position: show the newest turn.
    if (from === "voice" && why !== "voice_off" && why !== "server") noteInThread();
    if (typeof V.scrollDown === "function") V.scrollDown();
    // Chat is the safe side: ENGINE hears it at once (mic released, any countdown dropped, decision 5),
    // then the server (which disarms too). The next switch waits for this POST, so the server sees them in order.
    V.emit("mode:change", {mode: next, why});
    if (opts.post !== false) {
      const res = await within(postMode(next), POST_WAIT_MS);
      if (res.error) toast(`Chat is on here, but the server did not get the switch (${res.error}).`);
    }
    return mode;
  }
  // Voice: the server flag first, so ENGINE's first read-back or turn finds voice mode on.
  if (opts.post !== false) {
    const pending = postMode(next);
    const res = await within(pending, POST_WAIT_MS);
    if (res.missing || res.error) {
      // Without the server flag no spoken word can do anything: stay in Chat rather than look ready.
      applyClass(from);
      placeFocus(from);
      if (res.missing) voiceOff = true;
      toast(res.missing ? "Voice is off on this server, so Vaani stays in Chat." : `Could not switch to Voice (${res.error}). Vaani stays in Chat.`);
      return mode;
    }
    if (res.late) {
      pending.then(r => { if ((r.missing || r.error) && mode === "voice") fallBack(r.missing ? {off: true, missing: true} : {error: r.error}); });
    }
  }
  V.emit("mode:change", {mode: next, why});
  return mode;
}

function setMode(next, opts = {}) {
  if (next !== "voice" && next !== "chat") return Promise.reject(new Error(`unknown mode ${next}`));
  const why = opts.why || "user";
  if (why !== "voice_off" && why !== "server") userSwitched = true;
  const run = chain.then(() => switchTo(next, why, opts));
  chain = run.catch(e => console.error("Vaani mode switch failed", e));
  return run;
}

// Voice cannot work here: open (or go back to) Chat with one toast. No POST when the voice routes are missing.
function fallBack(p) {
  if (voiceOff && mode === "chat") return chain;   // already there for this cause: one toast
  voiceOff = true;
  toast(problemText(p, userSwitched ? "server" : "start"));
  return setMode("chat", {why: userSwitched ? "server" : "voice_off", post: !p.missing});
}

// A new chat starts in the current mode: tell the server (queued behind any switch in flight).
function postCurrent() {
  chain = chain.then(async () => {
    const m = mode;
    const res = await postMode(m);
    if (m !== "voice" || !(res.missing || res.error)) {
      if (res.error) toast(`Could not tell the server about Chat mode (${res.error}).`);
      return;
    }
    // queued after this step, so it never waits on itself
    fallBack(res.missing ? {off: true, missing: true} : {error: res.error});
  }).catch(e => console.error("Vaani mode post failed", e));
}

// ---------------------------------------------------------------- wiring

Object.defineProperty(V, "mode", {get: () => mode, configurable: true});
V.setMode = setMode;

V.on("ready", () => {
  mountToggles();
  const hud = document.getElementById("hud");
  if (hud && window.MutationObserver) new MutationObserver(mountToggles).observe(hud, {childList: true, subtree: true});
  syncToggles();
  if (mode === "chat") return;
  voiceProblem().then(p => { if (p && mode === "voice") fallBack(p); });
});

V.on("chat:new", () => { notedFor = null; postCurrent(); });
})();
