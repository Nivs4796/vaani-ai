/* Vaani AI web page ("Ledger"): an audit trail, not a chatbot.
 *
 * Every proposal shows exactly what will happen; every result shows the receipt Google gave.
 * Everything here degrades: a field or route the server does not have (404) hides that piece
 * of UI and the page keeps working as the classic Confirm / Cancel chat.
 * DOM is always built with createElement / textContent, never innerHTML.
 */
"use strict";
(() => {
const $ = s => document.querySelector(s);

// ---------------------------------------------------------------- shared helpers

let me = null;

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

function btn(label, onclick, cls, attrs) {
  return h("button", Object.assign({type: "button", class: cls || null, onclick}, attrs || {}), label);
}

async function api(path, body, method) {
  const opt = {method: method || (body !== undefined ? "POST" : "GET"), headers: {}, credentials: "same-origin"};
  if (body !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
  if (opt.method !== "GET" && me && me.csrf) opt.headers["X-CSRF-Token"] = me.csrf;
  const r = await fetch(path, opt);
  const j = await r.json().catch(() => ({}));
  if (r.status === 401) { location.href = "/login"; }
  if (!r.ok) { const e = new Error(j.error || `${path} failed (${r.status})`); e.status = r.status; throw e; }
  return j;
}

// A 404 that is "this route does not exist" (older server), not "unknown session".
const missingRoute = e => e && e.status === 404 && !/session/i.test(e.message || "");

function tzOpt() { return me && me.tz ? {timeZone: me.tz} : {}; }
function clock(d) {
  try { return d.toLocaleTimeString("en-GB", Object.assign({hour: "2-digit", minute: "2-digit", second: "2-digit"}, tzOpt())); }
  catch (_) { return d.toLocaleTimeString(); }
}
function dayKey(d) {
  try { return d.toLocaleDateString("en-GB", tzOpt()); } catch (_) { return d.toDateString(); }
}
function shortStamp(iso) {
  const d = new Date(iso);
  if (isNaN(d)) return String(iso || "");
  if (dayKey(d) === dayKey(new Date())) return clock(d);
  try { return d.toLocaleString("en-GB", Object.assign({day: "numeric", month: "short", hour: "2-digit", minute: "2-digit"}, tzOpt())); }
  catch (_) { return d.toLocaleString(); }
}
const plural = (n, one, many) => `${n} ${n === 1 ? one : (many || one + "s")}`;
// Voice mode (html.mode-voice, mode.js): the chat UI is hidden, so it must not take focus or keys.
const voiceMode = () => document.documentElement.classList.contains("mode-voice");

// "Ravi Kumar <ravi@x.com>" -> "Ravi"; a bare address stays as it is.
function who(addr) {
  const s = String(addr || "").trim();
  const m = s.match(/^"?([^"<]+?)"?\s*<[^>]+>$/);
  return m ? m[1].trim().split(/\s+/)[0] : s;
}

// ---------------------------------------------------------------- sign-in page

if (document.body.dataset.page === "login") {
  const err = new URLSearchParams(location.search).get("error");
  const line = $("#loginError");
  if (err && line) {
    const known = {access_denied: "Google sign-in was cancelled. Nothing was shared."};
    const sentence = err.charAt(0).toUpperCase() + err.slice(1) + (/[.!?]$/.test(err) ? "" : ".");
    line.textContent = known[err] || (/\s/.test(err) ? sentence : `Sign-in did not finish (${err}).`);
    line.hidden = false;
  }
  return;
}

// ---------------------------------------------------------------- chat page

const app = $("#app"), rail = $("#rail"), drawer = $("#drawer"), scrim = $("#scrim");
const log = $("#log"), scroller = $("#scroller"), box = $("#box"), send = $("#send"), form = $("#f");
const BOX_PLACEHOLDER = box.placeholder;
let sessionId = null, busy = false, briefCard = null, editing = null;

const WF = {replies: "Replies", scheduling: "Scheduling", follow_ups: "Follow-ups", triage: "Triage"};
const LEVELS = ["off", "suggest", "one_tap", "auto"];
const LEVEL_NAME = {off: "Off", suggest: "Suggest", one_tap: "One-tap", auto: "Auto"};
const wfName = k => WF[k] || k || "";
const levelName = k => LEVEL_NAME[k] || k || "";

function add(node) { log.appendChild(node); return node; }
function scrollDown() { scroller.scrollTop = scroller.scrollHeight; }
// Scroll the chat, never the page, so el's top sits just under the top of the thread.
// (scrollIntoView would also scroll the page itself, sliding the header and rail off screen.)
function scrollThreadTo(el, smooth) {
  const top = scroller.scrollTop + el.getBoundingClientRect().top - scroller.getBoundingClientRect().top - 12;
  const still = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  scroller.scrollTo({top: Math.max(0, top), behavior: smooth && !still ? "smooth" : "auto"});
}

// ---- reply renderer (unchanged rules from the classic page)
const IS_CAL = /^https:\/\/(?:www\.google\.com\/calendar\/|calendar\.google\.com\/)/;
// Replies arrive as light markdown from the model. Render a safe subset by building DOM
// nodes (never innerHTML): **bold**, numbered / bulleted lists, paragraphs, [label](url)
// links and bare URLs. Calendar links get a fixed label so a mangled URL is never shown.
const INLINE_RE = /\*\*([^*\n]+)\*\*|\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)|(https?:\/\/[^\s<>)\]]+)/g;
function anchor(url, label) {
  const a = h("a", {href: url, target: "_blank", rel: "noopener"});
  a.textContent = IS_CAL.test(url) ? "Open in Google Calendar" : label; return a;
}
function inline(target, text) {
  let last = 0; INLINE_RE.lastIndex = 0;
  for (const m of String(text).matchAll(INLINE_RE)) {
    if (m.index > last) target.appendChild(document.createTextNode(text.slice(last, m.index)));
    if (m[1] != null) { const b = document.createElement("strong"); inline(b, m[1]); target.appendChild(b); }
    else if (m[2] != null) target.appendChild(anchor(m[3], m[2]));
    else target.appendChild(anchor(m[4], m[4].replace(/[.,;:!?]+$/, "")));
    last = m.index + m[0].length;
  }
  if (last < text.length) target.appendChild(document.createTextNode(text.slice(last)));
}
const LIST_RE = /^(\s*)(?:(\d+)[.)]|[-*•])\s+(.*)$/;
// Lists: "1. Item" opens / continues an ordered list and keeps the model's own number (li.value),
// so a list interrupted by sub-bullets never restarts at 1. A bullet that is indented, or that
// follows a numbered item, is nested inside that item. A plain line ends the list.
function renderReply(target, text) {
  const lines = String(text || "").replace(/\r/g, "").split("\n");
  let top = null, sub = null, lastLi = null, para = null;
  const make = tag => document.createElement(tag);
  for (const raw of lines) {
    const line = raw.replace(/\s+$/, "");
    if (!line.trim()) { para = null; continue; }   // blank line: lists stay open, paragraphs end
    const m = line.match(LIST_RE);
    if (m) {
      const indent = m[1].replace(/\t/g, "  ").length, numbered = m[2] != null, tag = numbered ? "ol" : "ul";
      const li = make("li");
      if (numbered) li.value = parseInt(m[2], 10);
      inline(li, m[3]);
      para = null;
      const nest = lastLi && (indent >= 2 || (!numbered && top && top.tagName === "OL"));
      if (nest) {
        if (!sub || sub.tagName.toLowerCase() !== tag || sub.parentNode !== lastLi) { sub = make(tag); lastLi.appendChild(sub); }
        sub.appendChild(li);
        continue;
      }
      if (!top || top.tagName.toLowerCase() !== tag) { top = make(tag); target.appendChild(top); }
      top.appendChild(li); lastLi = li; sub = null;
      continue;
    }
    top = sub = lastLi = null;
    if (!para) { para = make("p"); target.appendChild(para); }
    else para.appendChild(make("br"));
    inline(para, line.replace(/^#{1,6}\s+/, ""));
  }
}

// ---------------------------------------------------------------- turns

function setBusy(b) {
  busy = b; box.disabled = b; send.disabled = b; $("#newBtn").disabled = b;
  document.querySelectorAll(".card.live button, [data-turn]").forEach(x => { if (!x.hasAttribute("data-off")) x.disabled = b; });
  if (!b && drawer.hidden && !app.classList.contains("rail-open") && !voiceMode()) box.focus();
}

function spinner(verb) {
  const t = h("span", {"aria-hidden": "true"}, `${verb}… 0.0s`);
  const s = h("div", {class: "spin"}, h("span", {class: "sr"}, `${verb}…`), t);
  const t0 = performance.now();
  s._tick = setInterval(() => { t.textContent = `${verb}… ${((performance.now() - t0) / 1000).toFixed(1)}s`; }, 100);
  s._stop = () => { clearInterval(s._tick); s.remove(); };
  return s;
}

// The dark "Putting together your brief" banner shown while /new or /brief builds the brief.
function briefBanner() {
  const s = h("div", {class: "brief-banner", role: "status"},
    h("span", {text: "Putting together your brief"}),
    h("span", {class: "dots", "aria-hidden": "true"}, h("i"), h("i"), h("i")));
  s._stop = () => s.remove();
  return s;
}

// One round trip that renders a TurnResult. opts: draft (the card being acted on), verb,
// fallback {path, payload} for a route an older server lacks, after(r), onFail(e).
async function turn(path, payload, label, opts = {}) {
  if (busy || !sessionId) return null;
  setBusy(true);
  const via = opts.via || (payload && payload.via) || "typed";
  if (label) add(h("div", {class: "msg user"}, label));
  const spin = add(spinner(opts.verb || "thinking"));
  window.Vaani.emit("turn:start", {path, payload, label, via});
  scrollDown();
  try {
    let r;
    const body = Object.assign({session_id: sessionId}, payload);
    try {
      // Live steps: stream.js answers with the same dict POST /chat returns (and falls back to it).
      // Voice mode's /voice/turn is a stream of the same shape (contract v3).
      const streamed = (path === "/chat" || path === "/voice/turn") && window.Vaani.streamTurn;
      r = streamed ? await window.Vaani.streamTurn(body, {spin, path}) : await api(path, body);
    } catch (e) {
      if (!(opts.fallback && missingRoute(e))) throw e;
      r = await api(opts.fallback.path, Object.assign({session_id: sessionId}, opts.fallback.payload));
    }
    spin._stop();
    renderTurn(r, opts);
    window.Vaani.emit("turn:result", {r, path, payload, via});
    return r;
  } catch (e) {
    spin._stop();
    window.Vaani.emit("turn:error", {error: e, path, payload, via});
    add(h("div", {class: "msg bot err"}, `Request failed: ${e.message}`));
    if (opts.onFail) opts.onFail(e);
    if (/unknown session/i.test(e.message)) { setBusy(false); await newChat(); }
    return null;
  } finally { setBusy(false); scrollDown(); }
}

function renderTurn(r, opts = {}) {
  if (r.reply) renderReply(add(h("div", {class: "msg bot"})), r.reply);
  const failed = (r.tool_calls || []).filter(t => !t.ok).length;
  if (r.footer) add(h("div", {class: "meta" + (failed ? " err" : "")}, r.footer + (failed ? ` · ${failed} tool error(s)` : "")));
  if (r.receipt) receiptCard(r.receipt, {auto: r.decision === "auto", draft: r.pending_draft || opts.draft || null});
  const d = r.pending_draft;
  retireLive(d && d.draft_id);
  if (d) (r.decision === "inline" ? inlineCard : draftCard)(d);
  if (r.offer) offerCard(r.offer);
  if (opts.after) opts.after(r);
  updateStrip(r);
  // A sent nudge closes a waiting thread; GET /followups reads Gmail, so only refresh then.
  if (r.receipt && ((opts.draft && opts.draft.workflow) || r.receipt.workflow) === "follow_ups") refreshFollowupCount();
}

// Only the newest card can be approved (the server refuses older ones too).
function retireLive(keepId) {
  for (const old of log.querySelectorAll(".card.live")) {
    const same = keepId && old.dataset.draft === keepId;
    old._done(same ? "Shown again below" : keepId ? "Replaced by a newer card" : "No longer waiting");
  }
}

// ---------------------------------------------------------------- cards

function chipFor(d) {
  if (!d || !d.workflow) return null;
  return h("span", {class: "chip accent"}, wfName(d.workflow) + (d.level ? ` · ${levelName(d.level)}` : ""));
}

function approveLabel(d, k) {
  const base = k.confirm_label || "Confirm";
  if (d.kind === "email_send" && base === "Send" && Array.isArray(d.to) && d.to.length)
    return `Send to ${who(d.to[0])}${d.to.length > 1 ? ` +${d.to.length - 1}` : ""}`;
  return base;
}

function cardOf(d) { return d.card || {title: `Draft: ${d.summary}`, lines: [], confirm_label: "Confirm", note: ""}; }

// Shared by the full and the compact card: marks it finished and disables its buttons.
function finisher(c, buttons, status) {
  return text => {
    c.classList.remove("live"); c.classList.add("done");
    buttons.forEach(b => { b.disabled = true; b.setAttribute("data-off", ""); });
    status.textContent = text;
    const stamp = c.querySelector(".stamp");
    const m = /^(Approved|Skipped)/.exec(text);
    if (stamp) { stamp.classList.remove("accent-ink"); stamp.textContent = m ? m[1] : "Closed"; }
    if (editing && editing === c.dataset.draft) endEdit();
  };
}

function approveTurn(d, label, done, variant) {
  done("Approved · waiting for Google");
  const payload = {confirm_draft_id: d.draft_id};
  if (variant) payload.confirm_variant = variant;
  turn("/chat", payload, label, {
    draft: d, verb: "carrying it out",
    after: r => done(r.receipt ? "Approved · receipt below" : "Approved · see the reply below"),
    onFail: () => done("Approved · the request failed, check Gmail or Calendar"),
  });
}

function draftCard(d) {
  const k = cardOf(d);
  const c = h("section", {class: "card action live" + (k.danger ? " danger" : ""), "data-draft": d.draft_id, "aria-label": `Waiting for your approval: ${k.title}`});
  c.append(h("div", {class: "card-head"}, h("span", {class: "stamp accent-ink"}, "Waiting for you"), chipFor(d)));
  c.append(h("h4", {text: k.title}));
  for (const line of k.lines || []) c.append(h("div", {class: "line", text: line}));
  if (k.body) c.append(h("div", {class: "mailbody", text: k.body}));
  if (k.note) c.append(h("p", {class: "note", text: k.note}));
  const label = approveLabel(d, k);
  const ok = btn(label, null, k.danger ? "danger" : "primary");
  const alt = k.alt ? btn(k.alt.label) : null;
  const edit = btn("Edit", null, "ghost", {title: "Tell Vaani what to change"});
  const skip = btn("Skip", null, "ghost");
  const status = h("span", {class: "status", role: "status"});
  const buttons = [ok, alt, edit, skip].filter(Boolean);
  c.append(h("div", {class: "actions"}, buttons, status));
  const done = finisher(c, buttons, status);
  c._done = done;
  // Check busy BEFORE marking the card done: a click during an in-flight request must change nothing.
  ok.onclick = () => { if (!busy) approveTurn(d, label, done); };
  if (alt) alt.onclick = () => { if (!busy) approveTurn(d, k.alt.label, done, k.alt.variant); };
  edit.onclick = () => { if (!busy) startEdit(d); };
  skip.onclick = () => {
    if (busy) return;
    done("Skipped · nothing was changed");
    turn("/skip", {draft_id: d.draft_id}, "Skip", {fallback: {path: "/chat", payload: {cancel_draft_id: d.draft_id}}});
  };
  return add(c);
}

// decision "inline" (One-tap): one line, one button.
function inlineCard(d) {
  const k = cardOf(d);
  const label = approveLabel(d, k);
  const ok = btn(label, null, k.danger ? "danger sm" : "primary sm");
  const skip = btn("Skip", null, "ghost sm");
  const status = h("span", {class: "status", role: "status"});
  const c = h("section", {class: "card action inline live" + (k.danger ? " danger" : ""), "data-draft": d.draft_id, "aria-label": `One tap to approve: ${k.title}`},
    chipFor(d), h("span", {class: "inline-title", title: (k.lines || []).join("\n") || null, text: k.title}), ok, skip, status);
  const done = finisher(c, [ok, skip], status);
  c._done = done;
  ok.onclick = () => { if (!busy) approveTurn(d, label, done); };
  skip.onclick = () => {
    if (busy) return;
    done("Skipped · nothing was changed");
    turn("/skip", {draft_id: d.draft_id}, "Skip", {fallback: {path: "/chat", payload: {cancel_draft_id: d.draft_id}}});
  };
  return add(c);
}

function receiptCard(rc, {auto, draft}) {
  const wf = (draft && draft.workflow) || rc.workflow;
  const kind = (draft && draft.kind) || "";
  const source = rc.source || (wf === "scheduling" || /^event/.test(kind) || rc.htmlLink ? "Google Calendar" : "Gmail");
  const failed = /ERROR|FAIL|NOT/i.test(rc.status || "");
  const verified = typeof rc.verified === "number" ? (rc.count ? rc.verified >= rc.count : rc.verified > 0) : rc.verified === true;
  const tone = failed ? "risk" : verified ? "ok" : "warn";
  const tick = failed ? "Did not complete" : verified ? `✓ Verified by ${source}` : `Not verified · check ${source}`;
  const c = h("section", {class: `card receipt ${tone}`, "aria-label": `Receipt from ${source}`});
  c.append(h("div", {class: "card-head"},
    h("span", {class: "stamp tone"}, `Receipt · ${source}`),
    auto ? h("span", {class: "chip accent", title: "Carried out without a card, as set in Autonomy"}, "Auto") : null,
    chipFor(wf ? {workflow: wf, level: draft && draft.level} : null),
    h("span", {class: "tick tone"}, tick),
    h("time", {class: "stamp when"}, clock(new Date()))));
  const report = (rc.report && rc.report.length) ? rc.report : [rc.status || "No report from Google"];
  const ul = h("ul", {class: "report"});
  for (const line of report) { const li = h("li"); inline(li, String(line)); ul.append(li); }
  c.append(ul);
  if (rc.htmlLink) c.append(h("p", {class: "note"}, anchor(rc.htmlLink, "Open in Google Calendar")));
  const undo = rc.undo || rc.undo_text;
  if (undo && typeof undo === "object" && undo.draft_id) {
    const b = btn(undo.label || "Undo", null, "ghost sm");
    b.onclick = () => {
      if (busy) return;
      b.disabled = true;
      doAction({action: "undo", draft_id: undo.draft_id}, "Undo that");
    };
    c.append(h("div", {class: "undo-row"}, b, undo.text ? h("span", {class: "note", text: undo.text}) : null));
  } else if (undo) c.append(h("p", {class: "undo", text: typeof undo === "string" ? undo : (undo.text || undo.label || "")}));
  return add(c);
}

// A promotion ("right N times in a row, move up?") or a step-back ("skipped N in a row, move down?").
function offerCard(o) {
  const back = o.kind === "step_back";
  const yes = o.accept_action || "accept", no = o.snooze_action || "snooze";
  const status = h("p", {class: "status", role: "status"});
  const accept = btn(o.accept_label || "Accept", null, back ? "sm" : "primary");
  const later = btn(o.snooze_label || "Not now", null, "ghost");
  const why = back ? `${plural(o.skips || 0, "skipped card")} in a row` : `${plural(o.streak || 0, "clean approval")} in a row`;
  const c = h("section", {class: "card offer" + (back ? " step-back" : ""), "aria-label": `${back ? "Step back" : "Offer"}: ${wfName(o.workflow)}`},
    h("div", {class: "card-head"}, h("span", {class: "stamp accent-ink"}, back ? "Step back?" : "Offer"), chipFor({workflow: o.workflow, level: o.from_level})),
    h("p", {class: "offer-text", text: o.text || `Move ${wfName(o.workflow)} from ${levelName(o.from_level)} to ${levelName(o.to_level)}?`}),
    h("p", {class: "note", text: `${why} · ${levelName(o.from_level)} → ${levelName(o.to_level)}`}),
    h("div", {class: "actions"}, accept, later), status);
  const go = async action => {
    accept.disabled = later.disabled = true;
    try {
      const row = await promote(o.workflow, action);
      status.textContent = action === yes ? `${wfName(o.workflow)} is now ${levelName((row && row.level) || o.to_level)}.`
        : back ? `Keeping ${levelName(o.from_level)}.` : "Okay, staying as it is. Vaani will ask again later.";
      c.classList.add("done");
    } catch (e) { status.textContent = e.message; status.classList.add("err"); accept.disabled = later.disabled = false; }
  };
  accept.onclick = () => go(yes);
  later.onclick = () => go(no);
  return add(c);
}

async function promote(workflow, action) {
  const row = await api(`/promotion/${encodeURIComponent(workflow)}`, {action});
  if (drawerKind === "autonomy") showAutonomy();
  window.Vaani.emit("autonomy:changed", {workflow, action, row});  // the Settings page redraws its ladder
  return row;
}

// ---------------------------------------------------------------- brief

const ROW_ACTIONS = {
  // The server reads the email and asks the model what the sender wants (sets the email in focus);
  // the user then says "reply saying ..." and gets a card.
  reply: {label: "Reply", run: row => doAction({action: "reply", message_id: row.id, thread_id: row.thread_id}, `What does ${((row.sender || "they").split(/\s+/)[0])} want?`)},
  nudge: {label: "Draft nudge", run: row => doAction({action: "nudge", message_id: row.id, thread_id: row.thread_id}, `Draft a nudge to ${row.sender || row.to_name || "them"}`)},
  dismiss: {label: "Dismiss", cls: "ghost sm", run: (row, li) => doAction({action: "dismiss", thread_id: row.thread_id, message_id: row.id},
    `Stop tracking “${row.subject || "this thread"}”`, r => { if (r && li) dismissRow(li); refreshFollowupCount(); })},
  open: {label: "Open", run: row => showEmail(row)},
};

function doAction(payload, label, after) {
  closeOverlays();
  return turn("/action", payload, label, {after});
}

function dismissRow(li) {
  li.classList.add("dismissed");
  li.querySelectorAll("button").forEach(b => { b.disabled = true; b.setAttribute("data-off", ""); });
}

function rowEl(row) {
  const li = h("li", {class: "row" + (row.unread ? " unread" : "")});
  const sender = row.sender || row.to_name || row.from || "Unknown sender";
  li.append(h("div", {class: "row-top"},
    row.unread ? h("span", {class: "dot", title: "Unread"}, h("span", {class: "sr"}, "Unread")) : null,
    h("span", {class: "sender", text: sender + (row.n > 1 ? ` ×${row.n}` : "")}),
    row.days_waiting != null ? h("span", {class: "chip warn", text: `waiting ${plural(row.days_waiting, "day")}`}) : null,
    h("span", {class: "date", text: row.date || ""})));
  li.append(h("div", {class: "subj", text: row.subject || "(no subject)"}));
  if (row.snippet) li.append(h("div", {class: "snip", text: row.snippet}));
  const acts = h("div", {class: "row-actions"});
  for (const a of row.actions || []) {
    const spec = ROW_ACTIONS[a];
    if (!spec) continue;
    const b = btn(spec.label, () => { if (!busy) spec.run(row, li); }, spec.cls || "sm", a === "open" ? null : {"data-turn": true});
    if (a !== "open" && busy) b.disabled = true;
    acts.append(b);
  }
  if (acts.childNodes.length) li.append(acts);
  return li;
}

const ROW_CAP = 5;
function groupEl(g) {
  const rows = g.rows || [];
  const sec = h("div", {class: `group g-${g.key}`});
  const n = g.count != null ? g.count : rows.length;
  sec.append(h("h4", {}, g.title || g.key, h("span", {class: "count", text: String(n)})));
  if (g.key === "promotions" || g.action === "archive_all" || (g.actions || []).includes("archive_all")) {
    const msgs = g.messages || n;
    if (!msgs) { sec.append(h("p", {class: "empty", text: g.empty_text || "Nothing here."})); return sec; }
    sec.append(h("div", {class: "promo"},
      h("span", {text: `${plural(msgs, "promotional email")} from ${plural(n, "sender")}`}),
      btn("Archive all", () => { if (!busy) doAction({action: "archive_all"}, `Archive all ${plural(msgs, "promotion")}`); }, "sm", {"data-turn": true, disabled: busy})));
    return sec;
  }
  if (!rows.length) { sec.append(h("p", {class: "empty", text: g.empty_text || "Nothing here."})); return sec; }
  const ul = h("ul", {class: "rows"});
  rows.forEach((row, i) => { const li = rowEl(row); if (i >= ROW_CAP) li.hidden = true; ul.append(li); });
  sec.append(ul);
  if (rows.length > ROW_CAP) {
    const more = btn(`Show ${rows.length - ROW_CAP} more`, () => { ul.querySelectorAll("li[hidden]").forEach(li => { li.hidden = false; }); more.remove(); }, "link");
    sec.append(more);
  }
  return sec;
}

function briefAge(b) {
  const t = Date.parse(b && b.built_at);
  return isNaN(t) ? null : Math.max(0, Math.floor((Date.now() - t) / 60000));
}

function updateStamp(c) {
  const b = c._brief || {}, mins = briefAge(b), parts = [];
  if (mins != null) parts.push(mins < 1 ? "Built just now" : `Built ${mins} min ago`);
  if (b.covers_days != null) parts.push(`covers ${plural(b.covers_days, "day")}`);
  if (b.count != null) parts.push(plural(b.count, "email"));
  if (b.unread != null) parts.push(`${b.unread} unread`);
  c._stamp.textContent = parts.join(" · ") + (parts.length ? " · " : "");
  const stale = mins != null && mins >= 30;
  c.classList.toggle("stale", stale);
  c._stamp.title = stale ? "Older than 30 minutes: refresh to see new mail" : "";
}

function fillBrief(c, b) {
  c.textContent = "";
  c._brief = b;
  c._stamp = h("span", {});
  const refresh = btn("Refresh", () => refreshBrief(c, refresh), "link");
  c.append(h("div", {class: "brief-head"}, h("h3", {}, "Inbox brief"), h("p", {class: "stamp-row"}, c._stamp, refresh)));
  if (b.error) c.append(h("p", {class: "err-line", text: `Could not build the brief: ${b.error}`}));
  for (const g of b.groups || []) c.append(groupEl(g));
  updateStamp(c);
}

function briefEl(b) {
  const c = h("section", {class: "card brief", "aria-label": "Inbox brief", tabindex: "-1"});
  fillBrief(c, b);
  return c;
}

async function refreshBrief(c, button) {
  if (button) button.disabled = true;
  c._stamp.textContent = "Refreshing from Gmail… ";
  try { fillBrief(c, await api(`/brief?refresh=1${sessionId ? `&session_id=${encodeURIComponent(sessionId)}` : ""}`)); }
  catch (e) { updateStamp(c); c.querySelector(".brief-head").after(h("p", {class: "err-line", text: `Refresh failed: ${e.message}`})); if (button) button.disabled = false; }
}

setInterval(() => document.querySelectorAll(".card.brief").forEach(c => c._brief && updateStamp(c)), 30000);

let briefRoute = true;
async function showBrief() {
  closeOverlays();
  if (briefCard && briefCard.isConnected) {
    // BRIEF's card says when it is stale and refreshes itself; the classic card is refreshed from here.
    if (briefCard.isStale && briefCard.isStale()) briefCard.refresh();
    else if (briefCard._stamp && (briefAge(briefCard._brief) || 0) >= 30) refreshBrief(briefCard);
    scrollThreadTo(briefCard, true);
    briefCard.focus({preventScroll: true});
    return;
  }
  const classic = () => turn("/chat", {text: "Check my latest emails and give me updates"}, "Check my latest emails and give me updates");
  if (!briefRoute || busy || !sessionId) return briefRoute ? null : classic();
  const b = $("#briefBtn"); b.disabled = true; b.setAttribute("aria-busy", "true");
  const spin = briefBanner();
  const head = log.querySelector(":scope > .meta");  // pinned first, under the "New chat" stamp
  if (head) head.after(spin); else log.prepend(spin);
  log.querySelectorAll(":scope > .hello").forEach(x => x.remove());
  try {
    // session_id keeps the rows' Reply / Archive matched to what is on screen.
    const brief = await api(`/brief?session_id=${encodeURIComponent(sessionId)}`);
    briefCard = (window.Vaani.renderBrief || briefEl)(brief);
    spin.replaceWith(briefCard);
    scrollThreadTo(briefCard, false);
    if (briefCard.focus) briefCard.focus({preventScroll: true});
    window.Vaani.emit("brief:shown", {brief, el: briefCard});
  } catch (e) {
    spin._stop();
    if (missingRoute(e)) { briefRoute = false; return classic(); }
    add(h("div", {class: "msg bot err"}, `Could not build the brief: ${e.message}`));
  } finally { b.disabled = busy; b.removeAttribute("aria-busy"); }
}

// ---------------------------------------------------------------- drawer

let drawerKind = null, drawerTrigger = null;
const narrow = w => window.matchMedia(`(max-width: ${w}px)`).matches;

function syncScrim() {
  const show = (app.classList.contains("rail-open") && narrow(899)) || (!drawer.hidden && narrow(1099));
  scrim.hidden = !show;
}

function openDrawer(kind, title, trigger) {
  drawerKind = kind;
  if (trigger) drawerTrigger = trigger;
  $("#drawerTitle").textContent = title;
  const body = $("#drawerBody");
  body.textContent = "";
  drawer.hidden = false;
  app.classList.add("with-drawer");
  closeRail();
  syncScrim();
  $("#drawerTitle").focus();
  return body;
}

function closeDrawer() {
  if (drawer.hidden) return;
  drawer.hidden = true;
  app.classList.remove("with-drawer");
  drawerKind = null;
  syncScrim();
  const t = drawerTrigger; drawerTrigger = null;
  if (t && t.isConnected && t.offsetParent !== null && !narrow(899)) t.focus(); else if (!voiceMode()) box.focus();
}

function openRail() { app.classList.add("rail-open"); $("#menuBtn").setAttribute("aria-expanded", "true"); syncScrim(); $("#newBtn").focus(); }
function closeRail() { if (!app.classList.contains("rail-open")) return; app.classList.remove("rail-open"); $("#menuBtn").setAttribute("aria-expanded", "false"); syncScrim(); }
// Overlays only: a docked drawer on a wide screen stays open while the chat works.
function closeOverlays() { closeRail(); if (narrow(1099)) closeDrawer(); }

function loading(parent) { return parent.appendChild(h("p", {class: "lede", text: "Loading…"})); }

// ---- email detail (placeholder until a full reader exists)
function showEmail(row) {
  const body = openDrawer("email", "Email", document.activeElement);
  const sender = row.sender || row.from || "Unknown sender";
  body.append(h("div", {class: "email-meta"},
    h("p", {class: "stamp", text: row.date || ""}),
    h("h3", {text: row.subject || "(no subject)"}),
    h("p", {class: "muted", text: `From ${sender}`})));
  if (row.snippet) body.append(h("blockquote", {class: "mailbody", text: row.snippet}));
  body.append(h("p", {class: "lede", text: "This is the preview from the brief. Ask Vaani to read the whole email; it reads it in Gmail and summarises it here."}));
  const acts = h("div", {class: "actions"},
    btn("Ask what they want", () => { closeOverlays(); turn("/chat", {text: `What does ${sender} want in "${row.subject || ""}"?`}, `What does ${sender} want in “${row.subject || ""}”?`); }, "primary", {"data-turn": true, disabled: busy}),
    (row.actions || []).includes("reply") ? btn("Reply", () => ROW_ACTIONS.reply.run(row), null, {"data-turn": true, disabled: busy}) : null);
  body.append(acts);
  if (row.thread_id) body.append(h("p", {class: "note"}, h("a", {href: `https://mail.google.com/mail/u/0/#all/${encodeURIComponent(row.thread_id)}`, target: "_blank", rel: "noopener"}, "Open in Gmail")));
}

// ---- follow-ups (the count lives in RAIL's side-menu section; this refreshes it and any open list)
let followRoute = true;
async function refreshFollowupCount() {
  refreshRail();
  if (!followRoute || !$("#followList")) return;
  try { const j = await api("/followups"); fillFollowups(j.rows || []); }
  catch (e) { if (missingRoute(e)) followRoute = false; }
}
function showFollowups(body) {
  body.append(h("p", {class: "lede", text: "Threads where you wrote last and nobody has answered yet."}));
  const list = body.appendChild(h("div", {id: "followList"}));
  loading(list);
  api("/followups").then(j => fillFollowups(j.rows || [], list))
    .catch(e => { const l = $("#followList"); if (l) { l.textContent = ""; l.append(h("p", {class: "inline-err", role: "alert", text: e.message})); } });
}
function fillFollowups(rows, list) {
  list = list || $("#followList");
  if (!list) return;
  list.textContent = "";
  if (!rows.length) { list.append(h("p", {class: "empty", text: "Nothing waiting. Every thread you started has an answer."})); return; }
  const ul = h("ul", {class: "rows"});
  for (const row of rows) {
    const li = h("li", {class: "row"});
    const name = row.to_name || row.sender || row.to_email || "them";
    li.append(h("div", {class: "row-top"}, h("span", {class: "sender", text: `To ${name}`}),
      row.days_waiting != null ? h("span", {class: "chip warn", text: `waiting ${plural(row.days_waiting, "day")}`}) : null));
    li.append(h("div", {class: "subj", text: row.subject || "(no subject)"}));
    if (row.snippet) li.append(h("div", {class: "snip", text: row.snippet}));
    const err = h("p", {class: "inline-err", role: "alert"});
    li.append(h("div", {class: "row-actions"},
      btn("Draft nudge", () => { if (!busy) doAction({action: "nudge", message_id: row.message_id || row.id, thread_id: row.thread_id}, `Draft a nudge to ${name}`); }, "sm", {"data-turn": true, disabled: busy}),
      btn("Dismiss", async () => {
        try { await api(`/followups/${encodeURIComponent(row.thread_id)}/dismiss`, {}); dismissRow(li); refreshFollowupCount(); }
        catch (e) { err.textContent = e.message; }
      }, "ghost sm")), err);
    ul.append(li);
  }
  list.append(ul);
}

// ---- autonomy
const WF_DESC = {
  replies: "Email replies Vaani writes for you.",
  scheduling: "Creating, moving and answering calendar events.",
  follow_ups: "Nudges on threads waiting for an answer.",
  triage: "Archive, mark read, label and star. Trash always asks.",
};
const LEVEL_DESC = {
  off: "Vaani does not propose these on its own. You can still ask.",
  suggest: "Vaani proposes a full card. Nothing happens until you approve it.",
  one_tap: "Vaani proposes a one-line card. One tap approves it.",
  auto: "Vaani carries it out and shows you Google's receipt.",
};
// Workflows whose Auto needs an explicit unlock (anything that is not reversible triage).
const UNLOCK = {
  replies: {label: "Send without asking", title: "Send replies without asking?",
    what: "When Replies is on Auto, Vaani will send the replies it writes from your Gmail without showing you a card first. Each one still gets a receipt from Gmail, and you can switch this off here at any time."},
  follow_ups: {label: "Send nudges without asking", title: "Send nudges without asking?",
    what: "When Follow-ups is on Auto, Vaani will send follow-up nudges from your Gmail on threads waiting for an answer, without showing you a card first. Each one still gets a receipt from Gmail, and you can switch this off here at any time."},
  scheduling: {label: "Change my calendar without asking", title: "Change your calendar without asking?",
    what: "When Scheduling is on Auto, Vaani will create and change events on your Google Calendar without showing you a card first. Guests may see the changes on their calendars. Each change still gets a receipt, and you can switch this off here at any time."},
};

function confirmDialog({title, text, ok, cancel}) {
  const dlg = $("#confirmDlg");
  if (!dlg || typeof dlg.showModal !== "function") return Promise.resolve(window.confirm(`${title}\n\n${text}`));
  $("#dlgTitle").textContent = title; $("#dlgText").textContent = text;
  $("#dlgOk").textContent = ok; $("#dlgCancel").textContent = cancel;
  dlg.returnValue = "";
  dlg.showModal();
  $("#dlgCancel").focus();
  return new Promise(resolve => dlg.addEventListener("close", () => resolve(dlg.returnValue === "ok"), {once: true}));
}

function wfRow(w0) {
  let w = w0, err = "", focusKey = null;
  const sec = h("section", {class: "wf", "aria-labelledby": `wf-${w0.key}`});
  const put = async (patch, key) => {
    focusKey = key;
    try { w = Object.assign({}, w, await api(`/autonomy/${encodeURIComponent(w.key)}`, patch, "PUT")); err = ""; }
    catch (e) { err = e.message; }
    render();
  };
  const render = () => {
    sec.textContent = "";
    const title = w.title || wfName(w.key);
    const ceil = LEVELS.indexOf(w.ceiling), maxIdx = ceil < 0 ? LEVELS.length - 1 : ceil;
    const unlock = UNLOCK[w.key];
    const lockedWhy = w.auto_allowed === false ? `Auto is not allowed for ${title} on this server.`
      : unlock && !w.send_unlocked ? `Switch on “${unlock.label}” below first.` : "";
    sec.append(h("div", {class: "wf-head"}, h("h3", {id: `wf-${w.key}`, text: title}),
      h("span", {class: `chip lv-${w.level}`, text: levelName(w.level)})));
    if (WF_DESC[w.key]) sec.append(h("p", {class: "wf-desc", text: WF_DESC[w.key]}));
    const seg = h("div", {class: "seg", role: "group", "aria-label": `${title}: how much Vaani may do`});
    LEVELS.forEach((lv, i) => {
      const locked = lv === "auto" && lockedWhy;
      const b = h("button", {type: "button", "data-key": lv, "aria-pressed": String(w.level === lv)},
        locked ? h("span", {class: "lock", "aria-hidden": "true"}, "🔒") : null, levelName(lv));
      if (i > maxIdx) { b.disabled = true; b.title = `Not available for ${title}`; }
      else if (locked) { b.setAttribute("aria-disabled", "true"); b.setAttribute("aria-label", "Auto (locked)"); b.title = lockedWhy; b.onclick = () => { err = lockedWhy; focusKey = lv; render(); }; }
      else if (lv !== w.level) b.onclick = () => put({level: lv}, lv);
      seg.append(b);
    });
    sec.append(seg, h("p", {class: "level-desc", text: LEVEL_DESC[w.level] || ""}));
    const next = LEVELS[LEVELS.indexOf(w.level) + 1];
    if ((w.level === "suggest" || w.level === "one_tap") && next && LEVELS.indexOf(next) <= maxIdx && w.n_needed) {
      const n = Math.min(w.n_needed, 10), s = Math.min(w.streak || 0, n), text = `${s} of ${n} toward ${levelName(next)}`;
      const meter = h("div", {class: "meter", role: "meter", "aria-valuemin": "0", "aria-valuemax": String(n), "aria-valuenow": String(s), "aria-label": text});
      for (let i = 0; i < n; i++) meter.append(h("span", {class: i < s ? "fill" : null}));
      sec.append(h("div", {class: "meter-row"}, meter, h("span", {text})));
      if (w.offer_due) {
        sec.append(h("div", {class: "actions tight"},
          h("span", {class: "offer-due", text: `Ready for ${levelName(next)}`}),
          btn("Accept", async () => { try { await promote(w.key, "accept"); } catch (e) { err = e.message; render(); } }, "primary sm"),
          btn("Not now", async () => { try { await promote(w.key, "snooze"); } catch (e) { err = e.message; render(); } }, "ghost sm")));
      }
    } else if (w.streak) {
      sec.append(h("div", {class: "meter-row"}, h("span", {text: `${plural(w.streak, "clean approval")} in a row`})));
    }
    if (unlock && w.auto_allowed !== false) {
      const sw = h("button", {type: "button", role: "switch", class: "switch", "data-key": "unlock", "aria-checked": String(!!w.send_unlocked), "aria-label": unlock.label},
        h("span", {class: "knob", "aria-hidden": "true"}), h("span", {text: unlock.label}));
      sw.onclick = async () => {
        if (w.send_unlocked) return put({send_unlocked: false}, "unlock");
        const yes = await confirmDialog({title: unlock.title, text: unlock.what, ok: unlock.label, cancel: "Keep asking"});
        if (yes) put({send_unlocked: true}, "unlock"); else sw.focus();
      };
      sec.append(sw);
    }
    if (err) sec.append(h("p", {class: "inline-err", role: "alert", text: err}));
    if (focusKey) { const f = sec.querySelector(`[data-key="${focusKey}"]`); if (f) f.focus(); focusKey = null; }
  };
  render();
  return sec;
}

function showAutonomy(body) {
  body = body || $("#drawerBody");
  body.textContent = "";
  body.append(h("p", {class: "lede", text: "How much Vaani may do on its own, for each kind of work. Every write still gets a receipt from Google."}));
  if (me && me.flags && me.flags.autonomy === false)
    body.append(h("p", {class: "notice", text: "Autonomy is switched off on this server, so every write waits for your approval whatever is set here."}));
  const list = body.appendChild(h("div", {class: "wf-list"}));
  loading(list);
  api("/autonomy").then(j => {
    list.textContent = "";
    for (const w of j.workflows || []) list.append(wfRow(w));
    if (!(j.workflows || []).length) list.append(h("p", {class: "empty", text: "No workflows reported by the server."}));
  }).catch(e => { list.textContent = ""; list.append(h("p", {class: "inline-err", role: "alert", text: e.message})); });
}

// ---- proof
function ago(iso) {
  const t = Date.parse(iso || "");
  if (Number.isNaN(t)) return "at an unknown time";
  const m = Math.max(0, Math.round((Date.now() - t) / 60000));
  if (m < 1) return "just now";
  if (m < 60) return `${m} min ago`;
  const hrs = Math.round(m / 60);
  return hrs < 48 ? `${hrs} h ago` : `${Math.round(hrs / 24)} days ago`;
}
const KIND_NAME = {email_send: "Email", email_modify: "Mailbox change", event_create: "New event", event_update: "Event change", event_delete: "Event delete", event_rsvp: "RSVP"};
// "Email" saved to drafts (the first of voice mode's two email steps) reads "Email draft"; unknown kinds are spelled out.
function kindName(r) {
  const k = r.card_kind || "";
  if (k === "email_send" && r.variant === "save_draft") return "Email draft";
  return KIND_NAME[k] || k.replace(/_/g, " ");
}
// "Approved by voice · “confirm” · 0.91": the words the Mac heard and how sure the speech model was.
function voiceOutcome(r) {
  const heard = String(r.heard || "").trim();
  const conf = r.stt_conf != null && r.stt_conf !== "" && isFinite(Number(r.stt_conf)) ? Number(r.stt_conf).toFixed(2) : "";
  const parts = ["Approved by voice", heard ? `“${heard.length > 60 ? heard.slice(0, 59) + "…" : heard}”` : "", conf];
  return parts.filter(Boolean).join(" · ");
}
function outcome(r) {
  const t = r.tool || "";
  if (t === "commit_pending" || t === "create_calendar_event") {
    if (!r.ok) return {text: "Failed", tone: "risk"};
    if (r.confirmed_by === "voice") return {text: voiceOutcome(r) + (r.edited ? " · edited" : ""), tone: r.verified ? "ok" : "warn"};
    const by = {policy: "Auto", auto: "Auto", inline: "One-tap", undo: "Undo"}[r.confirmed_by] || "Approved";
    return {text: by + (r.edited ? " · edited" : ""), tone: r.verified ? "ok" : "warn"};
  }
  if (t === "skip_pending") return {text: "Skipped", tone: "muted"};
  if (t === "promotion_accept") return {text: `Promotion accepted${r.level ? ` · ${levelName(r.level)}` : ""}`, tone: "accent"};
  if (t === "promotion_snooze") return {text: "Offer snoozed", tone: "muted"};
  if (t === "undo_applied") return {text: "Undone by you", tone: "warn"};
  if (t === "stepback_keep") return {text: "Kept level", tone: "muted"};
  if (t === "level_change") return {text: `Level changed${r.level ? ` · ${levelName(r.level)}` : ""}`, tone: "accent"};
  return {text: t.replace(/_/g, " ") || "Decision", tone: r.ok === false ? "risk" : "muted"};
}

function showProof(body, wf) {
  body = body || $("#drawerBody");
  body.textContent = "";
  body.append(h("p", {class: "lede", text: "The last 20 decisions, newest first. An approved write counts only when Vaani read it back from Google."}));
  const sel = h("select", {id: "proofWf", "aria-label": "Workflow"}, h("option", {value: ""}, "All workflows"),
    Object.keys(WF).map(k => h("option", {value: k, selected: k === wf}, WF[k])));
  sel.onchange = () => showProof(body, sel.value);
  body.append(h("div", {class: "proof-tools"}, sel, btn("Refresh", () => showProof(body, sel.value), "ghost sm")));
  const out = body.appendChild(h("div", {}));
  loading(out);
  api(`/proof?limit=20${wf ? `&workflow=${encodeURIComponent(wf)}` : ""}`).then(j => {
    out.textContent = "";
    const ev = j.evals;
    if (ev && ev.total) {
      const bad = (ev.suites || []).filter(x => !x.ok);
      out.append(h("div", {class: "evals " + (ev.all_passed ? "ok" : "warn"), "aria-label": "Offline checks"},
        h("p", {class: "evals-head"}, h("strong", {}, `Offline checks: ${ev.passed} of ${ev.total} passing`),
          h("span", {class: "note"}, ` · ${(ev.suites || []).length} suites · ran ${ago(ev.ran_at)}`)),
        h("p", {class: "note", text: bad.length ? `Failing: ${bad.map(x => `${x.name} ${x.passed}/${x.total}`).join(", ")}`
          : (ev.suites || []).map(x => `${x.name} ${x.passed}`).join(" · ")})));
    }
    const streaks = Object.entries(j.streaks || {});
    if (streaks.length) out.append(h("div", {class: "streaks", "aria-label": "Clean approvals in a row"},
      streaks.map(([k, n]) => h("span", {class: "chip" + (n ? " ok" : ""), title: "Clean approvals in a row"}, `${wfName(k)} · streak ${n}`))));
    const rows = j.rows || [];
    if (!rows.length) { out.append(h("p", {class: "empty", text: "No decisions yet. Approve or skip a card and it shows up here."})); return; }
    const ol = h("ol", {class: "proof-list"});
    for (const r of rows) {
      const o = outcome(r);
      const v = r.verified === true ? h("span", {class: "tick ok", title: "Verified with Google"}, "✓", h("span", {class: "sr"}, " verified"))
        : r.verified === false && r.ok !== false && /commit|create/.test(r.tool || "") ? h("span", {class: "tick warn", title: "Not verified"}, "!", h("span", {class: "sr"}, " not verified"))
        : h("span", {class: "tick muted", "aria-hidden": "true"}, "–");
      ol.append(h("li", {class: "proof-row"},
        h("time", {datetime: r.ts || null}, shortStamp(r.ts)),
        h("span", {class: "what"}, h("span", {}, [wfName(r.workflow), kindName(r)].filter(Boolean).join(" · ") || "—"),
          h("span", {class: `outcome ${o.tone}`}, o.text)),
        v,
        h("span", {class: "ms"}, r.ms != null ? `${r.ms} ms` : "")));
    }
    out.append(ol);
  }).catch(e => { out.textContent = ""; out.append(h("p", {class: "inline-err", role: "alert", text: e.message})); });
}

// ---------------------------------------------------------------- composer, strip, session

function startEdit(d) {
  editing = d.draft_id;
  box.placeholder = "Tell me what to change";
  form.classList.add("editing");
  closeOverlays();
  box.focus();
}
function endEdit() { editing = null; box.placeholder = BOX_PLACEHOLDER; form.classList.remove("editing"); }

const isLocal = footer => /\blocal\b/.test(footer || "") || (me && me.model_local === true);
function updateStrip(r) {
  const strip = $("#strip");
  strip.textContent = "";
  const parts = [];
  const model = (r && r.model) || (me && me.model);
  if (model) parts.push(h("span", {}, model));
  if (me && me.model_present === false) parts.push(h("span", {class: "warn"}, "model not loaded"));
  if (r) {
    if (r.latency_ms != null) parts.push(h("span", {}, `${(r.latency_ms / 1000).toFixed(1)}s`));
    const names = [...new Set((r.tool_calls || []).map(t => t.name).filter(Boolean))];
    parts.push(h("span", {}, names.length ? `tools: ${names.join(", ")}` : "no tools"));
    if (isLocal(r.footer)) parts.push(h("span", {class: "local"}, "mail stayed on this Mac"));
  } else parts.push(h("span", {}, "ready"));
  parts.forEach((p, i) => { if (i) strip.append(h("span", {class: "sep", "aria-hidden": "true"}, " · ")); strip.append(p); });
}

// Composer chips: four of these, a different four on each new chat.
const CHIPS = ["What's on my calendar today?", "Who's waiting on a reply from me?", "Find a free hour tomorrow afternoon",
  "Summarise my unread emails", "Draft a reply to my last email"];
let chipRound = Math.floor(Math.random() * CHIPS.length);
function renderChips() {
  const wrap = $("#composerChips");
  if (!wrap) return;
  chipRound = (chipRound + 1) % CHIPS.length;
  wrap.textContent = "";
  for (let i = 0; i < 4; i++) {
    const t = CHIPS[(chipRound + i) % CHIPS.length];
    wrap.append(btn(t, () => { if (!busy) window.Vaani.submit(t, {via: "chip"}); }, "chip-pill", {"data-turn": true, disabled: busy}));
  }
}

// An empty chat without a brief opens with a short greeting instead of a blank page.
function hello() {
  const u = (me && me.user) || {};
  const first = String(u.name || "").trim().split(/\s+/)[0];
  const hr = new Date().getHours();
  const greet = hr < 12 ? "Good morning" : hr < 17 ? "Good afternoon" : "Good evening";
  add(h("div", {class: "hello"},
    h("h1", {text: `${greet}${first ? `, ${first}` : ""}.`}),
    h("p", {text: "Ask about your inbox or your calendar, or pick a suggestion below."})));
}

function nowLabel() {
  try { return new Date().toLocaleString("en-GB", Object.assign({weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit"}, tzOpt())); }
  catch (_) { return new Date().toLocaleString(); }
}

function reauthBanner() {
  add(h("section", {class: "card banner warn", role: "alert"},
    h("p", {}, "Google asked you to sign in again. Vaani cannot read or change anything until you do."),
    h("div", {class: "actions"}, btn("Sign in again", () => { location.href = "/auth/login"; }, "primary"))));
}

async function newChat() {
  if (busy) return;
  log.textContent = ""; briefCard = null; sessionId = null; endEdit();
  if (drawerKind === "email") closeDrawer();
  if (me && me.reauth_required) reauthBanner();
  renderChips();
  setBusy(true);
  const spin = add(me && me.flags && me.flags.brief_on_open ? briefBanner() : spinner("starting a new chat"));
  try {
    const j = await api("/new", {});
    spin._stop();
    sessionId = j.session_id;
    add(h("div", {class: "meta", title: `Session ${sessionId}`}, `New chat · ${nowLabel()}`));
    if (j.brief) briefCard = add(window.Vaani.renderBrief ? window.Vaani.renderBrief(j.brief) : briefEl(j.brief));
    else hello();
    window.Vaani.emit("chat:new", {session_id: sessionId, brief: j.brief || null});
    if (j.brief) window.Vaani.emit("brief:shown", {brief: j.brief, el: briefCard});
  } catch (e) {
    spin._stop();
    add(h("div", {class: "msg bot err"}, `Could not start a chat: ${e.message}`));
  } finally { setBusy(false); }
  refreshFollowupCount();
}

function renderMe() {
  const line = $("#me");
  line.textContent = `Connected as ${me.email} · ${(me.scopes || []).join(", ")} · model: ${me.model} (local) · now: ${me.now} ${me.tz}`;
  if (me.model_present === false) { line.textContent += " · WARNING: model not loaded"; line.classList.add("warn"); }
  line.title = line.textContent;
  const u = me.user || {};
  const email = u.email || me.email || "";
  $("#acctName").textContent = u.name || (me.auth_mode === "google" ? "" : "This Mac");
  $("#acctEmail").textContent = email;
  const av = $("#avatar");
  av.textContent = (u.name || email || "?").trim().charAt(0).toUpperCase();
  if (u.picture) {
    const img = h("img", {src: u.picture, alt: "", referrerpolicy: "no-referrer"});
    img.onerror = () => img.remove();
    av.append(img);
  }
  $("#signOut").hidden = me.auth_mode !== "google";
  if (me.flags && me.flags.theme) document.body.classList.add(`theme-${me.flags.theme}`);
}

// ---------------------------------------------------------------- shell: storage, toast, splash, theme

const store = {
  get(k) { try { return localStorage.getItem(k); } catch (_) { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch (_) { /* private window: not remembered */ } },
};
const reducedMotion = () => window.matchMedia("(prefers-reduced-motion: reduce)").matches;

// Vaani.toast(text, {tone: "ok"|"warn"|"risk", action: {label, onClick}, ms}) -> {el, close}
function toast(text, opts = {}) {
  const wrap = $("#toasts");
  const ms = opts.ms == null ? 4000 : opts.ms;
  const t = h("div", {class: "toast" + (opts.tone ? ` ${opts.tone}` : "")},
    opts.tone ? h("span", {class: "toast-dot", "aria-hidden": "true"}) : null,
    h("span", {class: "toast-text", text: String(text == null ? "" : text)}));
  let timer = null;
  const close = () => {
    clearTimeout(timer);
    if (!t.isConnected) return;
    t.classList.add("out");
    setTimeout(() => t.remove(), reducedMotion() ? 0 : 200);
  };
  const arm = () => { clearTimeout(timer); if (ms > 0) timer = setTimeout(close, ms); };
  if (opts.action && opts.action.label) {
    t.append(btn(opts.action.label, e => {
      close();
      try { if (opts.action.onClick) opts.action.onClick(e); } catch (err) { console.error("toast action failed", err); }
    }, "toast-act"));
  }
  t.addEventListener("mouseenter", () => clearTimeout(timer));
  t.addEventListener("mouseleave", arm);
  wrap.append(t);
  while (wrap.children.length > 3) wrap.firstElementChild.remove();
  arm();
  return {el: t, close};
}

// First-load splash: cycles its status line, goes on chat:new or after 8 s, whichever is first.
const splash = $("#splash");
let splashTick = null;
function hideSplash() {
  if (!splash || !splash.isConnected) return;
  clearInterval(splashTick);
  splash.classList.add("out");
  setTimeout(() => splash.remove(), reducedMotion() ? 0 : 400);
}
if (splash) {
  const steps = ["SIGNING YOU IN", "READING YOUR INBOX", "ALMOST THERE"];
  let step = 0;
  splashTick = setInterval(() => {
    if (step < steps.length - 1) $("#splashStatus").textContent = steps[++step];
    else clearInterval(splashTick);
  }, 1600);
  setTimeout(hideSplash, 8000);
}

const themeBtn = $("#themeToggle");
function syncThemeBtn() {
  const dark = document.documentElement.getAttribute("data-theme") === "dark";
  const label = dark ? "Switch to light theme" : "Switch to dark theme";
  themeBtn.setAttribute("aria-label", label);
  themeBtn.title = label;
}
function toggleTheme() {
  const next = document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark";
  document.documentElement.setAttribute("data-theme", next);
  store.set("vaani.theme", next);
  syncThemeBtn();
}

// Desktop rail collapse (html.rail-collapsed; index.html applies the saved state before paint).
function setRailCollapsed(on) {
  document.documentElement.classList.toggle("rail-collapsed", on);
  store.set("vaani.rail", on ? "collapsed" : "open");
}
const railCollapsed = () => document.documentElement.classList.contains("rail-collapsed");

// ---------------------------------------------------------------- shell: rail sections (Vaani.rail.sections)

const railBox = $("#railSections");
const RAIL_OPEN_KEY = "vaani.rail.open";
let railOpen = {};
try { railOpen = JSON.parse(store.get(RAIL_OPEN_KEY) || "{}") || {}; } catch (_) { railOpen = {}; }
const railEls = new Map();   // id -> {sec, el, body, count, loadedAt, seq}
let railReady = false, railTimer = null;

// Section icons by name; anything else (a short string) is shown as text, a Node as itself.
const ICONS = {
  calendar: "M2.5 4.5h11v9h-11z M2.5 7.5h11 M5.5 2.5v3 M10.5 2.5v3",
  mail: "M2.5 4h11v8.5h-11z M2.5 4.5L8 9l5.5-4.5",
  inbox: "M2.5 9.5l1.5-5.5h8l1.5 5.5v3.5h-11z M2.5 9.5h3.5l.8 1.5h2.4l.8-1.5h3.5",
  reply: "M6.5 4L3 7.5 6.5 11 M3 7.5h6a4 4 0 0 1 4 4V13",
  clock: "M8 2.5a5.5 5.5 0 1 0 0 11a5.5 5.5 0 1 0 0-11z M8 5v3.2l2 1.3",
  history: "M2.8 8a5.2 5.2 0 1 0 1.6-3.8 M2.5 2.8v2.6h2.6 M8 5.2v3l2 1.2",
  chat: "M2.5 3.5h11v7.5H6.5L3.5 13.5v-2.5h-1z",
  draft: "M10.5 2.8l2.7 2.7-7.4 7.4-3.3.6.6-3.3z",
  bell: "M4 11V7.5a4 4 0 0 1 8 0V11l1 1.5H3z M6.8 14h2.4",
  star: "M8 2.2l1.8 3.7 4 .6-2.9 2.8.7 4L8 11.4l-3.6 1.9.7-4L2.2 6.5l4-.6z",
  check: "M3 8.5l3.2 3L13 4.5",
  send: "M2.5 8L13.5 2.5 10.5 13.5 7.5 9z M7.5 9l6-6.5",
  flag: "M3.5 14V2.5 M3.5 3h8l-1.8 3 1.8 3h-8",
  list: "M5.5 4.5h8 M5.5 8h8 M5.5 11.5h8 M2.5 4.5h.01 M2.5 8h.01 M2.5 11.5h.01",
  people: "M6 7.5a2.5 2.5 0 1 0 0-5a2.5 2.5 0 1 0 0 5z M1.8 13.5a4.2 4.2 0 0 1 8.4 0 M10.5 3a2.3 2.3 0 0 1 0 4.4 M12 9.6a4 4 0 0 1 2.2 3.9",
};
const ICON_ALIAS = {followups: "reply", follow_ups: "reply", meetings: "calendar", event: "calendar", drafts: "draft",
  edit: "draft", pencil: "draft", chats: "chat", email: "mail", recent: "history", time: "clock"};
const SVG_NS = "http://www.w3.org/2000/svg";
function svgIcon(d, size) {
  const svg = document.createElementNS(SVG_NS, "svg");
  for (const [k, v] of Object.entries({width: size, height: size, viewBox: "0 0 16 16", "aria-hidden": "true"})) svg.setAttribute(k, String(v));
  const p = document.createElementNS(SVG_NS, "path");
  for (const [k, v] of Object.entries({d, fill: "none", stroke: "currentColor", "stroke-width": "1.4", "stroke-linecap": "round", "stroke-linejoin": "round"})) p.setAttribute(k, v);
  svg.append(p);
  return svg;
}
function iconEl(icon) {
  if (icon instanceof Node) return icon.cloneNode(true);
  const key = String(icon || "").toLowerCase();
  const d = ICONS[key] || ICONS[ICON_ALIAS[key]];
  return d ? svgIcon(d, 16) : document.createTextNode(icon ? String(icon) : "");
}

function setRailCount(rec, n) {
  const has = typeof n === "number" && isFinite(n);
  rec.count.hidden = !has;
  rec.count.textContent = has ? String(n) : "";
  rec.count.classList.toggle("zero", n === 0);
}

function railOrder() {
  for (const s of window.Vaani.rail.sections) { const r = s && railEls.get(String(s.id)); if (r) railBox.append(r.el); }
}

function railMount(sec) {
  if (!railBox || !sec || sec.id == null || railEls.has(String(sec.id))) return null;
  const id = String(sec.id), bodyId = `rail-sec-${id.replace(/[^\w-]/g, "_")}`;
  const open = railOpen[id] !== undefined ? !!railOpen[id] : sec.open !== false;
  const count = h("span", {class: "rail-count", hidden: true});
  const head = h("button", {type: "button", class: "rail-sec-head", "aria-expanded": String(open), "aria-controls": bodyId},
    h("span", {class: "rail-sec-icon", "aria-hidden": "true"}, iconEl(sec.icon)),
    h("span", {class: "rail-sec-title", text: sec.title || id}), count,
    h("span", {class: "rail-chev", "aria-hidden": "true"}, svgIcon("M5.5 6.5L8 9l2.5-2.5", 14)));
  const body = h("div", {class: "rail-sec-body", id: bodyId, hidden: !open});
  const el = h("section", {class: "rail-sec" + (open ? " open" : ""), "data-id": id}, head, body);
  const rec = {sec, el, body, count, loadedAt: 0, seq: 0};
  head.onclick = () => {
    const now = head.getAttribute("aria-expanded") !== "true";
    head.setAttribute("aria-expanded", String(now));
    body.hidden = !now;
    el.classList.toggle("open", now);
    railOpen[id] = now;
    store.set(RAIL_OPEN_KEY, JSON.stringify(railOpen));
  };
  railEls.set(id, rec);
  railBox.append(el);
  railOrder();
  if (typeof sec.render === "function") {
    try {
      const out = sec.render(body, {setCount: n => setRailCount(rec, n), refresh: () => railLoad(rec)});
      if (out instanceof Node && out !== body && out.parentNode !== body) body.append(out);
    } catch (e) {
      console.error(`Rail section ${id} failed to render`, e);
      body.append(h("p", {class: "rail-empty err", text: "Could not show this section."}));
    }
  }
  return rec;
}

// load() -> {count, items: [{title, sub, meta, onClick}], empty}. A section with render() only gets its count from load().
async function railLoad(rec) {
  const sec = rec.sec;
  if (typeof sec.load !== "function") return;
  const seq = ++rec.seq;
  rec.loadedAt = Date.now();
  let res;
  try { res = await sec.load(); }
  catch (e) {
    if (seq !== rec.seq) return;
    if (missingRoute(e)) { rec.el.hidden = true; return; }
    if (typeof sec.render !== "function") { rec.body.textContent = ""; rec.body.append(h("p", {class: "rail-empty err", text: "Could not load right now."})); }
    return;
  }
  if (seq !== rec.seq || !res) return;
  rec.el.hidden = false;
  const items = Array.isArray(res.items) ? res.items : [];
  setRailCount(rec, res.count != null ? Number(res.count) : (Array.isArray(res.items) ? items.length : null));
  if (typeof sec.render === "function") return;
  rec.body.textContent = "";
  if (!items.length) { rec.body.append(h("p", {class: "rail-empty", text: res.empty || "Nothing here yet."})); return; }
  const ul = h("ul", {class: "rail-list"});
  for (const it of items) {
    const parts = [
      h("span", {class: "ri-main"}, h("span", {class: "ri-title", text: String(it.title || "")}),
        it.sub ? h("span", {class: "ri-sub", text: String(it.sub)}) : null),
      it.meta != null && it.meta !== "" ? h("span", {class: "ri-meta", text: String(it.meta)}) : null];
    const row = typeof it.onClick === "function"
      ? h("button", {type: "button", class: "rail-item", title: it.title ? String(it.title) : null, onclick: e => {
          if (narrow(899)) closeRail();
          try { it.onClick(it, e); } catch (err) { console.error("rail item failed", err); }
        }}, parts)
      : h("div", {class: "rail-item static"}, parts);
    ul.append(h("li", {}, row));
  }
  rec.body.append(ul);
}

// Reload every section's load(): at once, or debounced 1 s (chat:new, turn:result, refreshFollowupCount).
function refreshRail(now) {
  clearTimeout(railTimer);
  const run = () => {
    if (!railReady) return;
    for (const rec of railEls.values()) if (Date.now() - rec.loadedAt > 800) railLoad(rec);
  };
  if (now) run(); else railTimer = setTimeout(run, 1000);
}

window.Vaani.on("ready", () => {
  railReady = true;
  for (const s of window.Vaani.rail.sections) railMount(s);
  refreshRail(true);
});
window.Vaani.on("rail:register", s => {
  if (!railReady) return;   // mounted on ready with the rest
  const rec = railMount(s);
  if (rec) railLoad(rec);
});
window.Vaani.on("chat:new", () => { refreshRail(); hideSplash(); });
window.Vaani.on("turn:result", () => refreshRail());

// ---------------------------------------------------------------- wiring

function fitBox() {
  box.style.height = "auto";
  box.style.height = `${Math.min(box.scrollHeight, 220)}px`;
}
box.addEventListener("input", fitBox);
box.addEventListener("keydown", e => {
  // Enter sends, Shift+Enter is a new line; Enter that confirms an IME composition does neither.
  if (e.key !== "Enter" || e.shiftKey || e.isComposing || e.keyCode === 229) return;
  e.preventDefault();
  if (form.requestSubmit) form.requestSubmit(); else send.click();
});
form.onsubmit = e => {
  e.preventDefault();
  const t = box.value.trim();
  if (!t) return;
  box.value = ""; fitBox(); endEdit();
  turn("/chat", {text: t}, t);
};

const jump = $("#jumpBtn");
// Shown once the chat has a message and the view sits more than 200px above the newest one.
const syncJump = () => {
  jump.hidden = !log.querySelector(".msg.user") || scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight <= 200;
};
scroller.addEventListener("scroll", syncJump, {passive: true});
if (window.ResizeObserver) new ResizeObserver(syncJump).observe(log);
jump.onclick = () => { scroller.scrollTo({top: scroller.scrollHeight, behavior: reducedMotion() ? "auto" : "smooth"}); box.focus(); };

$("#newBtn").onclick = () => { closeOverlays(); newChat(); };
$("#briefBtn").onclick = () => { if (!busy) showBrief(); };
$("#settingsBtn").onclick = () => {
  closeRail();
  if (typeof window.Vaani.openSettings === "function") window.Vaani.openSettings();
  else toast("Settings are not available yet.");
};
themeBtn.onclick = toggleTheme;
syncThemeBtn();
new MutationObserver(syncThemeBtn).observe(document.documentElement, {attributes: true, attributeFilter: ["data-theme"]});  // Settings flips it too
$("#railCollapse").onclick = () => { setRailCollapsed(true); $("#menuBtn").focus(); };
$("#drawerClose").onclick = closeDrawer;
// Narrow screens: the menu button opens the off-canvas rail. Wide screens: it brings a collapsed rail back.
$("#menuBtn").onclick = () => {
  if (!narrow(899)) { setRailCollapsed(false); $("#newBtn").focus(); return; }
  if (app.classList.contains("rail-open")) closeRail(); else openRail();
};
scrim.onclick = () => { closeRail(); closeDrawer(); };
window.addEventListener("resize", syncScrim);
$("#signOut").onclick = async () => {
  try { await api("/auth/logout", {}); } catch (_) { /* signed out or not, the login page is next */ }
  location.href = "/login";
};
document.addEventListener("keydown", e => {
  // Voice mode: Esc belongs to the voice engine (stop / cancel a countdown), not to the hidden chat UI.
  if (e.key !== "Escape" || voiceMode() || $("#confirmDlg").open) return;
  if (app.classList.contains("rail-open")) { closeRail(); $("#menuBtn").focus(); }
  else if (!drawer.hidden) closeDrawer();
  else if (editing) endEdit();
});

// ---------------------------------------------------------------- hook bus (coordinator-owned)
// Everything feature files may call. Keep these names working when refactoring app.js;
// add new ones freely. Contract: .build/CONTRACT-v2.md.
Object.assign(window.Vaani, {
  h, btn, api, add, scrollDown, setBusy, turn, renderTurn, renderReply, newChat, showBrief,
  doAction, showEmail, confirmDialog, refreshFollowupCount, shortStamp, clock, dayKey, plural, who,
  wfName, levelName, WF, LEVELS, toast,
  panels: {autonomy: showAutonomy, proof: showProof, followups: showFollowups},
  // A chat turn through the normal path, exactly as if typed. via: "typed" | "voice" | "chip".
  submit(text, opts = {}) {
    const t = String(text || "").trim();
    if (!t) return Promise.resolve(null);
    const via = opts.via || "typed";
    return turn("/chat", {text: t, via}, t, {via});
  },
});
// Live values: Object.assign would copy a getter's value once, so these are defined as real getters.
Object.defineProperties(window.Vaani, {
  me: {get: () => me, configurable: true},
  sessionId: {get: () => sessionId, configurable: true},
  busy: {get: () => busy, configurable: true},
});

(async () => {
  updateStrip(null);
  try {
    me = await api("/me");
    renderMe();
    updateStrip(null);
  } catch (e) {
    if (e.status === 401) return;  // api() is already on its way to /login
    $("#me").textContent = `Not connected: ${e.message}`;
    toast(`Not connected: ${e.message}`, {tone: "risk", ms: 8000});
  }
  window.Vaani.emit("ready", {me});
  await newChat();
  hideSplash();
})();
})();
