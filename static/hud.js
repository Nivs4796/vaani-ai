/* hud.js: the voice-mode "Jarvis HUD" (contract .build/CONTRACT-v3.md, "HUD DOM").
 *
 * Builds everything inside the empty <div id="hud">: header (brand, #modeSlotHud for MODE's
 * Chat|Voice toggle, clock, mute, theme), the stage (side panels, #orbMount for CORE, the
 * phase label, heard line, live caption, one focus card, hint, End voice session, the
 * TAP TO WAKE overlay) and the COMMS LOG. It only renders: ENGINE (handsfree.js) drives the
 * phases through voice:* events, CORE owns the orb, MODE shows and hides #hud.
 * The static DOM is built as soon as this file runs, so #modeSlotHud and #orbMount exist for
 * MODE and CORE whenever they look; data, timers and the orb mount start on "ready".
 * Panels read GET /meetings/today, the session brief and GET /voice/status; a failing route
 * leaves a quiet placeholder, never an error.
 */
"use strict";
(() => {
  const V = window.Vaani;
  if (!V) return;

  const MAX_LOG = 200;           // log entries kept on the page
  const CLOCK_MS = 15000;        // header clock (and the "in N min" line) refresh
  const PANELS_MS = 60000;       // panels refresh while in voice mode
  const AFTER_TURN_MS = 1500;    // panels refresh after a turn result (debounced)
  const LOG_TEXT_MAX = 320;      // a log line longer than this is cut at a word

  const LABELS = {
    asleep: "STANDBY", standby: "STANDBY", listening: "LISTENING", working: "WORKING",
    speaking: "SPEAKING", approval: "AWAITING YOUR VOICE", countdown: "SENDING · SAY STOP", done: "DONE · VERIFIED",
  };
  const HINTS = {
    asleep: "Tap to wake Vaani",
    standby: "Say “Hey Vaani”, or tap the core",
    listening: "Listening · try “next”, “read it” or “reply saying …”",
    working: "Working on it…",
    speaking: "Say “stop” or tap the core to interrupt",
    approval: "Waiting for your voice · say one of the words on the card",
    countdown: "Say “stop” to cancel",
    done: "Done",
  };
  const LIVE = new Set(["listening", "approval", "countdown"]);
  const TRY = ["brief me", "next", "read it", "reply saying …", "confirm", "send it", "stop", "undo",
    "switch to chat", "goodnight Vaani"];
  const CARD_TONE = {email: "acc", meeting: "acc", approval: "amber", count: "red", receipt: "green"};
  const SVG_NS = "http://www.w3.org/2000/svg";

  const st = {
    phase: "asleep", muted: false, heard: "", caption: "",
    wake: true, stt: "", meetings: null, brief: null, built: false, started: false,
  };
  const el = {};
  const inflight = {};
  let refreshTimer = 0;

  // ---------------------------------------------------------------- DOM helpers

  function h(tag, attrs, ...kids) {
    const e = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v == null || v === false) continue;
      if (k === "class") e.className = v;
      else if (k === "text") e.textContent = v;
      else if (k.startsWith("on") && typeof v === "function") e.addEventListener(k.slice(2), v);
      else e.setAttribute(k, v === true ? "" : String(v));
    }
    for (const c of kids.flat()) if (c != null && c !== false) e.append(c);
    return e;
  }

  // Small stroke icons (18 px, currentColor), as in the mock-up.
  function icon(paths, extra) {
    const svg = document.createElementNS(SVG_NS, "svg");
    for (const [k, v] of Object.entries({width: 18, height: 18, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor",
      "stroke-width": 1.6, "stroke-linecap": "round", "stroke-linejoin": "round", "aria-hidden": "true"})) svg.setAttribute(k, v);
    for (const d of paths) { const p = document.createElementNS(SVG_NS, "path"); p.setAttribute("d", d); svg.append(p); }
    if (extra) svg.append(extra);
    return svg;
  }
  const ICONS = {
    speaker: () => icon(["M11 5 6 9H3v6h3l5 4z", "M15.5 8.5a5 5 0 0 1 0 7M18.5 5.5a9 9 0 0 1 0 13"]),
    muted: () => icon(["M11 5 6 9H3v6h3l5 4z", "M22 9l-6 6M16 9l6 6"]),
    sun: () => {
      const c = document.createElementNS(SVG_NS, "circle");
      c.setAttribute("cx", 12); c.setAttribute("cy", 12); c.setAttribute("r", 4);
      return icon(["M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"], c);
    },
    moon: () => icon(["M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"]),
  };

  function panel(id, title) {
    const head = h("div", {class: "p-h", text: title});
    const body = h("div", {class: "p-body"});
    return {el: h("section", {id, class: "hud-panel", "aria-label": title.charAt(0) + title.slice(1).toLowerCase()}, head, body), head, body};
  }

  function row(k, valueEl) {
    return h("div", {class: "p-row"}, h("span", {class: "k", text: k}), valueEl);
  }

  // ---------------------------------------------------------------- build (once)

  function build() {
    const root = document.getElementById("hud");
    if (!root || st.built) return;
    st.built = true;
    el.root = root;
    root.textContent = "";
    root.dataset.phase = st.phase;

    // Header
    el.clock = h("span", {id: "hudClock", class: "hud-clock"});
    el.mute = h("button", {type: "button", id: "hudMute", class: "hud-icon", onclick: onMute});
    el.theme = h("button", {type: "button", id: "hudTheme", class: "hud-icon", onclick: onTheme});
    const top = h("header", {class: "hud-top"},
      h("div", {class: "hud-brand"}, h("span", {text: "VAANI"}), h("span", {class: "hud-sub", text: "VOICE"})),
      h("span", {id: "modeSlotHud", class: "mode-slot"}),
      h("div", {class: "hud-right"}, el.clock, el.mute, el.theme));

    // Side panels
    const next = panel("hudNext", "NEXT");
    const inbox = panel("hudInbox", "INBOX · LAST 3 DAYS");
    const sys = panel("hudSystem", "SYSTEM");
    const tryP = panel("hudTry", "TRY SAYING");
    el.nextBody = next.body;
    el.inboxHead = inbox.head;
    el.inboxBody = inbox.body;
    el.model = h("span", {text: "local"});
    el.speech = h("span", {text: "on this Mac"});
    el.mic = h("span", {class: "live", text: "○ OFF"});
    el.wakeWord = h("span", {text: "“Hey Vaani”"});
    sys.body.append(row("Model", el.model), row("Speech", el.speech), row("Mic", el.mic),
      row("Wake word", el.wakeWord), row("Approvals", h("span", {text: "by voice"})));
    tryP.body.append(h("div", {class: "hud-chips"}, TRY.map(t => h("span", {class: "hud-chip", text: t}))));
    placeholder(el.nextBody, "Loading your calendar…");
    placeholder(el.inboxBody, "Reading your inbox…");

    // Core column
    el.orb = h("div", {id: "orbMount", class: "orb-mount"});
    el.state = h("div", {id: "hudState", class: "hud-state", role: "status", "aria-live": "polite"}, LABELS.asleep);

    // Talk, focus, foot
    el.heard = h("div", {id: "hudHeard", class: "heard", hidden: true});
    el.caption = h("p", {id: "hudCaption", class: "caption", hidden: true});
    el.idle = h("p", {id: "hudIdle", class: "idle", text: "Vaani is asleep."});
    el.focus = h("div", {id: "hudFocus", class: "hud-focus", hidden: true});
    el.hint = h("span", {id: "hudHint", class: "hint", text: HINTS.asleep});
    el.end = h("button", {type: "button", id: "hudEnd", class: "hud-end", onclick: onEnd}, "End voice session");

    // Wake overlay (shown while asleep)
    el.wakeBtn = h("button", {type: "button", id: "hudWakeBtn", class: "wake-btn", "aria-label": "Tap to wake Vaani",
      "aria-describedby": "hudWakeText", onclick: onWake}, "TAP TO WAKE");
    el.wake = h("div", {id: "hudWake", class: "hud-wake"},
      h("div", {class: "wake-in"}, el.wakeBtn, h("h1", {text: "Vaani is ready"}),
        h("p", {id: "hudWakeText", text: "Voice first. One tap per visit lets Vaani use your mic and speaker. After that, just say “Hey Vaani”."})));

    const stage = h("section", {id: "hudStage", class: "hud-stage", "aria-label": "Voice"},
      h("div", {class: "stage-top"},
        h("div", {class: "side"}, next.el, inbox.el),
        h("div", {class: "core-wrap"}, el.orb, el.state),
        h("div", {class: "side"}, sys.el, tryP.el)),
      h("div", {class: "talk"}, el.heard, el.caption, el.idle),
      el.focus,
      h("div", {class: "stage-foot"}, el.hint, el.end),
      el.wake);

    // Comms log
    el.logEmpty = h("p", {class: "log-empty", text: "Your conversation with Vaani appears here, line by line."});
    el.logList = h("div", {id: "hudLogList", class: "log-list", role: "log", "aria-live": "polite",
      "aria-label": "Conversation with Vaani"}, el.logEmpty);
    const log = h("aside", {id: "hudLog", class: "hud-log", "aria-label": "Conversation log"},
      h("div", {class: "log-h"}, h("span", {text: "COMMS LOG"}), h("span", {text: "THIS CONVERSATION"})),
      el.logList);

    root.append(top, h("div", {class: "hud-main"}, stage, log));
    renderMute();
    renderTheme();
    tickClock();
    V.hud = {el: root};
  }

  function placeholder(body, text) {
    body.textContent = "";
    body.append(h("div", {class: "p-big", text: "—"}), h("div", {class: "p-sub", text}));
  }

  // ---------------------------------------------------------------- time

  function tzOpt() { const tz = V.me && V.me.tz; return tz ? {timeZone: tz} : {}; }
  function fmt(d, opts) {
    try { return d.toLocaleString("en-GB", Object.assign({}, opts, tzOpt())); }
    catch (_) { return d.toLocaleString("en-GB", opts); }  // unknown time zone: the browser's own
  }
  const hm = d => fmt(d || new Date(), {hour: "2-digit", minute: "2-digit", hourCycle: "h23"});

  function tickClock() {
    if (!el.clock) return;
    const d = new Date();
    const day = fmt(d, {weekday: "short", day: "2-digit", month: "short"}).replace(/,/g, "").toUpperCase();
    el.clock.textContent = `${hm(d)}  ·  ${day}`;
    if (st.meetings) renderNext();  // keeps "in 18 min" current
  }

  // "2026-10-14T11:00+05:30" (already in the user's time zone) -> "11:00".
  function hhmm(iso) {
    const m = /T(\d{2}):(\d{2})/.exec(String(iso || ""));
    if (m) return `${m[1]}:${m[2]}`;
    const t = Date.parse(iso);
    return isNaN(t) ? "" : hm(new Date(t));
  }

  function inMinutes(min) {
    if (min <= 0) return "starting now";
    if (min < 60) return `in ${min} min`;
    const hrs = Math.floor(min / 60), rest = min % 60;
    return `in ${hrs} h${rest ? ` ${rest} min` : ""}`;
  }

  // ---------------------------------------------------------------- panels

  function renderNext() {
    const body = el.nextBody;
    if (!body) return;
    const j = st.meetings;
    if (!j) { placeholder(body, "Loading your calendar…"); return; }
    const now = Date.now();
    const events = (Array.isArray(j.events) ? j.events : []).filter(e => e && !e.all_day);
    if (j.error && !events.length) { placeholder(body, "Calendar not available right now"); return; }
    const upcoming = events.filter(e => {
      let end = Date.parse(e.end || "");
      if (isNaN(end)) end = Date.parse(e.start || "") + 30 * 60000;
      return isNaN(end) || end > now;
    });
    body.textContent = "";
    if (!upcoming.length) {
      body.append(h("div", {class: "p-big", text: "Nothing else today"}));
      const allDay = (j.events || []).filter(e => e && e.all_day).length;
      if (allDay) body.append(h("div", {class: "p-sub", text: `${allDay} all-day event${allDay === 1 ? "" : "s"}`}));
      return;
    }
    const [first, ...rest] = upcoming;
    const start = Date.parse(first.start || "");
    const title = String(first.title || "(no title)");
    let when = "";
    if (!isNaN(start)) when = start > now ? inMinutes(Math.ceil((start - now) / 60000)) : `now · until ${hhmm(first.end)}`;
    const sub = [when, first.where].filter(Boolean).join(" · ");
    body.append(h("div", {class: "p-big", text: [hhmm(first.start), title].filter(Boolean).join(" · ")}));
    if (sub) body.append(h("div", {class: "p-sub", text: sub}));
    if (rest.length) {
      body.append(h("ul", {class: "p-list"}, rest.slice(0, 2).map(e =>
        h("li", {}, h("span", {class: "mono", text: hhmm(e.start)}), String(e.title || "(no title)")))));
    }
  }

  function shortSubject(s) {
    let t = String(s || "").trim();
    while (/^(re|fwd?|fw)\s*:\s*/i.test(t)) t = t.replace(/^(re|fwd?|fw)\s*:\s*/i, "");
    if (t.length <= 30) return t || "(no subject)";
    const cut = t.slice(0, 30);
    return cut.slice(0, cut.lastIndexOf(" ") > 14 ? cut.lastIndexOf(" ") : 30).replace(/[\s,;:.-]+$/, "") + "…";
  }
  function senderName(s) { return String(s || "?").replace(/\s*<[^>]*>\s*/g, " ").replace(/^"|"$/g, "").trim() || "?"; }

  function renderInbox() {
    const body = el.inboxBody;
    if (!body) return;
    const b = st.brief;
    if (!b) { placeholder(body, "Reading your inbox…"); return; }
    const days = Number(b.covers_days) || 3;
    el.inboxHead.textContent = days === 1 ? "INBOX · LAST DAY" : `INBOX · LAST ${days} DAYS`;
    if (b.error) { placeholder(body, "Inbox not available right now"); return; }
    const group = key => (Array.isArray(b.groups) ? b.groups : []).find(g => g && g.key === key) || null;
    const needs = group("needs_reply");
    const waiting = group("follow_ups");
    const n = needs ? Number(needs.count) || 0 : 0;
    const w = waiting ? Number(waiting.count) || 0 : 0;
    body.textContent = "";
    body.append(h("div", {class: "p-big", text: n ? `${n} need${n === 1 ? "s" : ""} a reply` : "Nothing needs a reply"}));
    const sub = [w ? `${w} waiting on others` : "", `${Number(b.unread) || 0} unread`].filter(Boolean).join(" · ");
    body.append(h("div", {class: "p-sub", text: sub}));
    const rows = needs && Array.isArray(needs.rows) ? needs.rows.slice(0, 3) : [];
    if (rows.length) {
      body.append(h("ul", {class: "p-list"}, rows.map(r =>
        h("li", {title: `${senderName(r.sender)} · ${r.subject || ""}`},
          r.unread ? h("span", {class: "unread", "aria-label": "unread", role: "img"}) : null,
          `${senderName(r.sender)} · ${shortSubject(r.subject)}`))));
    }
  }

  function renderSystem() {
    if (!el.model) return;
    const model = V.me && V.me.model;
    el.model.textContent = model ? `${model} · local` : "local";
    el.speech.textContent = st.stt === "off" ? "off" : st.stt === "loading" ? "loading…" : st.stt === "error" ? "not available" : "on this Mac";
    el.wakeWord.textContent = st.wake ? "“Hey Vaani”" : "off";
    renderMic();
  }

  function renderMic() {
    if (!el.mic) return;
    const p = st.phase;
    el.mic.textContent = LIVE.has(p) ? "● LIVE" : p === "asleep" ? "○ OFF" : st.wake ? "○ WAKE WORD ONLY" : "○ TAP TO TALK";
  }

  const isVoice = () => (V.mode ? V.mode === "voice" : document.documentElement.classList.contains("mode-voice"));

  async function once(key, fn) {
    if (inflight[key]) return;
    inflight[key] = true;
    try { await fn(); } catch (_) { /* quiet: the panel keeps its last value or placeholder */ } finally { inflight[key] = false; }
  }

  function loadMeetings() {
    return once("meetings", async () => {
      if (!V.api) return;
      try { st.meetings = await V.api("/meetings/today"); }
      catch (_) { if (!st.meetings) st.meetings = {error: "unavailable", events: []}; }
      renderNext();
    });
  }

  function loadInbox() {
    return once("inbox", async () => {
      // GET /brief without a session rebuilds from Gmail; wait for chat:new to give the session.
      const sid = V.sessionId;
      if (!sid || !V.api) return;
      try {
        const b = await V.api(`/brief?session_id=${encodeURIComponent(sid)}`);
        if (b && typeof b === "object" && (Array.isArray(b.groups) || b.error)) st.brief = b;
      } catch (_) { /* keep the last brief */ }
      renderInbox();
    });
  }

  function loadStatus() {
    return once("status", async () => {
      if (!V.api) return;
      try {
        const s = await V.api("/voice/status");
        st.wake = !(s && s.wake === "off");
        st.stt = (s && s.stt) || "";
      } catch (_) { /* keep the defaults */ }
      renderSystem();
    });
  }

  function refresh(withStatus) {
    if (!isVoice()) return;
    loadMeetings();
    loadInbox();
    if (withStatus) loadStatus();
  }
  function refreshSoon() {
    clearTimeout(refreshTimer);
    refreshTimer = setTimeout(() => refresh(false), AFTER_TURN_MS);
  }

  // ---------------------------------------------------------------- phase, talk, focus

  function setPhase(d) {
    if (!el.root) return;
    d = d || {};
    const phase = String(d.phase || "standby");
    st.phase = phase;
    el.root.dataset.phase = phase;
    let label = d.label;
    if (!label) label = phase === "countdown" && d.count != null ? `SENDING IN ${d.count} · SAY STOP` : (LABELS[phase] || phase.toUpperCase());
    if (el.state.textContent !== label) el.state.textContent = label;
    el.hint.textContent = d.hint || HINTS[phase] || "";
    renderMic();
    const asleep = phase === "asleep";
    if (el.wake.hidden === asleep) {
      const hadFocus = el.wake.contains(document.activeElement);
      el.wake.hidden = !asleep;
      if (hadFocus && !asleep) focusCore();
    }
    renderTalk();
  }

  function focusCore() {
    const target = (el.orb && el.orb.querySelector("button")) || el.end;
    try { target.focus({preventScroll: true}); } catch (_) { target.focus(); }
  }

  function renderTalk() {
    if (!el.heard) return;
    el.heard.hidden = !st.heard;
    el.heard.textContent = st.heard ? `› “${st.heard}”` : "";
    el.caption.hidden = !st.caption;
    el.caption.textContent = st.caption;
    el.idle.hidden = !!(st.heard || st.caption);
    el.idle.textContent = st.phase === "asleep" ? "Vaani is asleep." : "Standing by.";
  }

  function setHeard(d) {
    st.heard = String((d && d.text) || "").trim();
    if (el.heard) el.heard.classList.toggle("ignored", !!(d && d.ignored));
    renderTalk();
  }

  function setCaption(d) {
    st.caption = String((d && d.text) || "").trim();
    renderTalk();
  }

  function setFocus(card) {
    if (!el.focus) return;
    el.focus.textContent = "";
    if (!card || typeof card !== "object" || !(card.title || card.label)) { el.focus.hidden = true; return; }
    const kind = ["email", "meeting", "approval", "count", "receipt"].includes(card.kind) ? card.kind : "email";
    const tone = ["acc", "amber", "red", "green"].includes(card.tone) ? card.tone : CARD_TONE[kind];
    const say = Array.isArray(card.say) ? card.say.filter(Boolean).map(String) : [];
    el.focus.append(h("article", {class: `hud-card k-${kind} ct-${tone}`, "aria-label": card.label || card.title},
      card.label ? h("div", {class: "c-label", text: String(card.label)}) : null,
      card.title ? h("div", {class: "c-title", text: String(card.title)}) : null,
      card.sub ? h("div", {class: "c-sub", text: String(card.sub)}) : null,
      card.quote ? h("p", {class: "c-quote", text: String(card.quote)}) : null,
      say.length ? h("div", {class: "say-row"}, h("span", {class: "say-k", text: "SAY"}),
        say.map(w => h("span", {class: "say", text: w}))) : null,
      card.fine ? h("p", {class: "c-fine", text: String(card.fine)}) : null));
    el.focus.hidden = false;
  }

  // ---------------------------------------------------------------- comms log

  // Markdown, links, URLs, email addresses and ids out; the first n sentences in.
  function plain(text) {
    return String(text || "")
      .replace(/```[\s\S]*?```/g, " ")
      .replace(/!\[[^\]]*\]\([^)]*\)/g, " ")
      .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
      .replace(/<?\bhttps?:\/\/[^\s>)]+>?/gi, " ")
      .replace(/<?[\w.+-]+@[\w-]+(?:\.[\w-]+)+>?/g, " ")
      .replace(/\b(?:id|draft|draft_id|message|msg|thread)\s*[:=#]\s*\S+/gi, " ")
      .replace(/\b(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{12,}\b/gi, " ")
      .replace(/^\s{0,3}(?:#{1,6}|>|[-*+•]|\d+[.)])\s+/gm, "")
      .replace(/[*_`~#]+/g, "")
      .replace(/\(\s*\)/g, " ")
      .replace(/\s+/g, " ")
      .replace(/\s+([.,!?;:])/g, "$1")
      .trim();
  }
  function firstSentences(text, n) {
    const s = plain(text);
    if (!s) return "";
    const parts = s.match(/.*?[.!?]+["”’)\]]*(?=\s|$)|.+$/g) || [s];
    return parts.slice(0, n).join(" ").replace(/\s+/g, " ").trim();
  }
  function clip(s) {
    s = String(s || "").replace(/\s+/g, " ").trim();
    if (s.length <= LOG_TEXT_MAX) return s;
    const cut = s.slice(0, LOG_TEXT_MAX);
    return cut.slice(0, Math.max(cut.lastIndexOf(" "), LOG_TEXT_MAX - 40)) + "…";
  }

  function logLine(who, text, tag, tone) {
    if (!el.logList) return;
    text = clip(text);
    tag = String(tag || "").trim();
    if (!text && !tag) return;
    if (el.logEmpty.parentNode) el.logEmpty.remove();
    const you = String(who || "").toLowerCase() === "you";
    const t = ["green", "amber", "red"].includes(tone) ? tone : "green";
    el.logList.append(h("div", {class: `entry who-${you ? "you" : "vaani"}`},
      h("span", {class: "e-time", text: hm()}),
      h("div", {class: "e-body"},
        h("span", {class: "e-who", text: you ? "YOU" : "VAANI"}),
        text ? h("p", {class: "e-text", text}) : null,
        tag ? h("span", {class: `e-tag tone-${t}`, text: tag}) : null)));
    while (el.logList.children.length > MAX_LOG) el.logList.firstElementChild.remove();
    el.logList.scrollTop = el.logList.scrollHeight;
  }

  function verified(rc) {
    if (typeof rc.verified === "number") return rc.count ? rc.verified >= rc.count : rc.verified > 0;
    return rc.verified === true;
  }

  // The tag under a VAANI line: countdown, voice approval, stopped, or the receipt of a tap.
  function tagFor(r, path) {
    const v = (r && r.voice) || null;
    const rc = r && r.receipt && typeof r.receipt === "object" ? r.receipt : null;
    const failed = !!rc && /ERROR|FAIL|NOT/i.test(rc.status || "");
    if (v && v.countdown) return {text: `● ${Number(v.countdown.seconds) || 5}-SECOND COUNTDOWN`, tone: "red"};
    if (v && v.approved) {
      if (failed) return {text: "■ DID NOT COMPLETE", tone: "red"};
      if (rc && !verified(rc)) return {text: "✓ APPROVED BY VOICE · NOT VERIFIED", tone: "amber"};
      return {text: "✓ APPROVED BY VOICE · VERIFIED", tone: "green"};
    }
    const f = v && v.focus;
    if ((path === "/voice/disarm" && v) || (f && f.kind === "receipt" && f.tone === "amber" && /NOTHING SENT|NOT SENT|STOPPED/i.test(f.label || ""))) {
      return {text: "■ NOT SENT", tone: "amber"};
    }
    if (rc) {
      if (failed) return {text: "■ DID NOT COMPLETE", tone: "red"};
      const ok = verified(rc);
      const how = v ? "" : r.decision === "auto" ? "DONE AUTOMATICALLY · " : "APPROVED WITH A TAP · ";
      return {text: `✓ ${how}${ok ? "VERIFIED" : "NOT VERIFIED"}`, tone: ok ? "green" : "amber"};
    }
    return null;
  }

  function onResult(d) {
    const r = d && d.r;
    if (!r || typeof r !== "object") return;
    const v = r.voice && typeof r.voice === "object" ? r.voice : null;
    let text = v && v.spoken ? String(v.spoken) : firstSentences(r.reply, 2);
    if (!text && r.receipt) {
      const rep = Array.isArray(r.receipt.report) ? r.receipt.report[0] : "";
      text = firstSentences(rep || r.receipt.status || "", 1);
    }
    const tag = tagFor(r, d.path);
    logLine("vaani", text, tag && tag.text, tag && tag.tone);
    // Without ENGINE nothing else would show the result's focus card.
    if (v && "focus" in v && !V.hands) setFocus(v.focus);
    if (isVoice()) refreshSoon();
  }

  // ---------------------------------------------------------------- controls

  function onWake() {
    // Must stay synchronous inside the click: the browser only unlocks audio and the mic here.
    const hands = V.hands;
    if (hands && typeof hands.wake === "function") {
      try { hands.wake(); } catch (e) { console.error("Vaani.hands.wake failed", e); }
    } else if (V.toast) V.toast("Voice is not available right now. Switch to Chat to keep going.", {tone: "warn"});
  }

  function onEnd() {
    const hands = V.hands;
    if (hands && typeof hands.endSession === "function") {
      try { hands.endSession(); } catch (e) { console.error("Vaani.hands.endSession failed", e); }
    }
  }

  function onMute() {
    const hands = V.hands;
    const next = !(hands ? hands.muted : st.muted);
    if (hands && typeof hands.setMuted === "function") {
      try { hands.setMuted(next); } catch (e) { console.error("Vaani.hands.setMuted failed", e); }
      st.muted = !!hands.muted;
    } else st.muted = next;
    renderMute();
  }

  function renderMute() {
    if (!el.mute) return;
    const label = st.muted ? "Unmute Vaani" : "Mute Vaani";
    el.mute.textContent = "";
    el.mute.append(st.muted ? ICONS.muted() : ICONS.speaker());
    el.mute.setAttribute("aria-label", label);
    el.mute.setAttribute("aria-pressed", String(st.muted));
    el.mute.title = label;
  }

  const isDark = () => document.documentElement.getAttribute("data-theme") === "dark";
  function onTheme() {
    const next = isDark() ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    try { localStorage.setItem("vaani.theme", next); } catch (_) { /* private window: this visit only */ }
    renderTheme();
  }
  function renderTheme() {
    if (!el.theme) return;
    const dark = isDark();
    const label = dark ? "Switch to light theme" : "Switch to dark theme";
    el.theme.textContent = "";
    el.theme.append(dark ? ICONS.sun() : ICONS.moon());
    el.theme.setAttribute("aria-label", label);
    el.theme.title = label;
  }

  // ---------------------------------------------------------------- wiring

  build();

  V.on("voice:phase", setPhase);
  V.on("voice:heard", setHeard);
  V.on("voice:caption", setCaption);
  V.on("voice:focus", d => setFocus(d && typeof d === "object" && "card" in d ? d.card : d));
  V.on("voice:say", d => { if (d) logLine(d.who === "you" ? "you" : "vaani", d.text, d.tag, d.tone); });
  V.on("voice:mute", d => { st.muted = !!(d && d.muted); renderMute(); });
  V.on("turn:start", d => { if (d && d.label) logLine("you", String(d.label)); });
  V.on("turn:result", onResult);
  V.on("turn:error", () => logLine("vaani", "Sorry, that request failed.", "", ""));
  V.on("chat:new", d => {
    st.brief = (d && d.brief) || null;
    renderInbox();
    if (!st.brief) loadInbox();
  });
  V.on("brief:shown", d => { if (d && d.brief) { st.brief = d.brief; renderInbox(); } });
  V.on("mode:change", d => { if (d && d.mode === "voice") refresh(true); });

  V.on("ready", () => {
    build();
    if (st.started) return;
    st.started = true;
    if (V.orb && typeof V.orb.mount === "function") {
      try { V.orb.mount(el.orb); } catch (e) { console.error("Vaani.orb.mount failed", e); }
    }
    if (V.hands && typeof V.hands.muted === "boolean") { st.muted = V.hands.muted; renderMute(); }
    renderSystem();
    tickClock();
    setInterval(tickClock, CLOCK_MS);
    setInterval(() => { if (!document.hidden) refresh(true); }, PANELS_MS);
    document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(false); });
    new MutationObserver(renderTheme).observe(document.documentElement, {attributes: true, attributeFilter: ["data-theme"]});
    refresh(true);
    // Keyboard users land on the one button that matters while Vaani is asleep.
    setTimeout(() => {
      const a = document.activeElement, app = document.getElementById("app");
      if (isVoice() && st.phase === "asleep" && (!a || a === document.body || (app && app.contains(a)))) {
        try { el.wakeBtn.focus({preventScroll: true}); } catch (_) { /* ignore */ }
      }
    }, 0);
  });
})();
