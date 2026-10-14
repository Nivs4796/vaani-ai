/* Inbox brief (BRIEF): the open page that starts a chat, Maaya style, not a boxed card.
 *
 * Vaani.renderBrief(brief) -> HTMLElement, where brief = flow.public_brief():
 * {built_at, covers_days, count, unread, error?, groups: [{key, title, count, messages, rows, empty_text, action?}]}.
 * Layout: greeting sentence (.brief-greeting) + empty .brief-voice-slot (VOICE-CLIENT fills it),
 * a TODAY line, the built stamp with Refresh, then one accordion per group. Every number shown
 * comes from the brief JSON; nothing is estimated. Row buttons run the same /action turns the
 * old card did. Refresh re-renders in place and emits brief:shown again for the new brief.
 * The element also exposes el.refresh() and el.isStale() for showBrief.
 * DOM via createElement / textContent only. Contract: .build/CONTRACT-v2.md ("Brief DOM").
 */
"use strict";
(() => {
const V = window.Vaani;
if (!V) return;

const LISTED_MAX = 20;  // brief.MAX_RESULTS: a listing this long may have more mail behind it
const ROW_CAP = 5;      // rows shown per group before "Show N more"
const STALE_MIN = 30;   // minutes before the stamp turns amber
const OPEN_BY_DEFAULT = new Set(["needs_reply", "follow_ups"]);
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
let seq = 0;

// Helpers come from app.js, which loads after this file: look them up at call time.
const h = (...a) => V.h(...a);
const plural = (n, one, many) => V.plural(n, one, many);
const num = v => { const x = Number(v); return Number.isFinite(x) && x > 0 ? Math.floor(x) : 0; };
const span = days => days === 1 ? "last day" : `last ${days} days`;
const groupOf = (b, key) => (b.groups || []).find(g => g && g.key === key) || null;
const entries = g => g ? (g.count != null ? num(g.count) : (g.rows || []).length) : 0;
const isPromo = g => g.key === "promotions" || g.action === "archive_all" || (g.actions || []).includes("archive_all");
// What the accordion counts: emails for promotions (it has no rows), entries for the rest.
const shownCount = g => isPromo(g) ? num(g.messages != null ? g.messages : g.count) : entries(g);

// ---------------------------------------------------------------- time, in the user's zone

function tzOpt() { const me = V.me; return me && me.tz ? {timeZone: me.tz} : {}; }

function hourNow() {
  try { return Number(new Intl.DateTimeFormat("en-GB", Object.assign({hour: "numeric", hourCycle: "h23"}, tzOpt())).format(new Date())); }
  catch (_) { return new Date().getHours(); }
}

function greetWord() {
  const hr = hourNow();
  return hr >= 5 && hr < 12 ? "Good morning" : hr >= 12 && hr < 17 ? "Good afternoon" : "Good evening";
}

// "2026-10-14" for today (0) or yesterday (1) in the user's zone.
function dayStamp(back) {
  try {
    return new Intl.DateTimeFormat("en-CA", Object.assign({year: "numeric", month: "2-digit", day: "2-digit"}, tzOpt()))
      .format(new Date(Date.now() - back * 864e5));
  } catch (_) { return ""; }
}

function todayLabel() {
  try { return new Date().toLocaleDateString("en-GB", Object.assign({weekday: "short", day: "numeric", month: "short"}, tzOpt())); }
  catch (_) { return ""; }
}

// Row dates arrive as "YYYY-MM-DD HH:MM" in the user's zone: today shows the time, then "Yesterday", then "13 Oct".
function rowDate(s) {
  const m = String(s || "").match(/^(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2}))?/);
  if (!m) return String(s || "");
  const key = `${m[1]}-${m[2]}-${m[3]}`;
  if (key === dayStamp(0)) return m[4] ? `${m[4]}:${m[5]}` : "Today";
  if (key === dayStamp(1)) return "Yesterday";
  return `${Number(m[3])} ${MONTHS[Number(m[2]) - 1] || m[2]}`;
}

function ageMin(b) {
  const t = Date.parse(b && b.built_at);
  return isNaN(t) ? null : Math.max(0, Math.floor((Date.now() - t) / 60000));
}

// ---------------------------------------------------------------- sentences

function firstName() {
  const u = V.me && V.me.user;
  const name = String((u && u.name) || "").trim();
  return name ? name.split(/\s+/)[0] : "";
}

function greetingText(b) {
  const name = firstName();
  const hello = `${greetWord()}${name ? `, ${name}` : ""}.`;
  if (b.error) return `${hello} I couldn't read your inbox just now.`;
  if (!b.built_at) return hello;
  const days = num(b.covers_days) || 3, count = num(b.count), needs = entries(groupOf(b, "needs_reply"));
  if (!count) return `${hello} No new email in the ${span(days)}.`;
  const mail = `${count}${count >= LISTED_MAX ? "+" : ""} ${count === 1 ? "email" : "emails"} in the ${span(days)}`;
  return `${hello} ${mail}; ${needs ? `${needs} ${needs === 1 ? "needs" : "need"} a reply` : "none need a reply"}.`;
}

function todayText(b) {
  const parts = [];
  if (b.unread != null) {  // the server counts up to 100 unread for the query
    const unread = num(b.unread);
    parts.push(unread ? `${unread >= 100 ? "100+" : unread} unread` : "nothing unread");
  }
  const fu = groupOf(b, "follow_ups"), waiting = entries(fu);
  if (waiting) {
    const longest = Math.max(0, ...(fu.rows || []).map(r => num(r.days_waiting)));
    parts.push(`${plural(waiting, "follow-up")} waiting` + (longest ? ` (longest ${plural(longest, "day")})` : ""));
  }
  const worth = entries(groupOf(b, "worth_knowing"));
  if (worth) parts.push(`${worth} worth knowing`);
  const promo = groupOf(b, "promotions"), promos = promo ? shownCount(promo) : 0;
  if (promos) parts.push(plural(promos, "promotion"));
  const s = parts.join(" · ");
  return s.charAt(0).toUpperCase() + s.slice(1);
}

// ---------------------------------------------------------------- rows (same actions as the old card)

function dismissRow(li) {
  li.classList.add("dismissed");
  li.querySelectorAll("button").forEach(b => { b.disabled = true; b.setAttribute("data-off", ""); });
}

const ACTIONS = {
  // The server reads the email and asks the model what the sender wants (sets the email in focus).
  reply: {label: "Reply", run: row => V.doAction({action: "reply", message_id: row.id, thread_id: row.thread_id},
    `What does ${((row.sender || "they").split(/\s+/)[0])} want?`)},
  nudge: {label: "Draft nudge", run: row => V.doAction({action: "nudge", message_id: row.id, thread_id: row.thread_id},
    `Draft a nudge to ${row.sender || row.to_name || "them"}`)},
  dismiss: {label: "Dismiss", quiet: true, run: (row, li) => V.doAction({action: "dismiss", thread_id: row.thread_id, message_id: row.id},
    `Stop tracking “${row.subject || "this thread"}”`, r => { if (r && li) dismissRow(li); V.refreshFollowupCount(); })},
  open: {label: "Open", run: row => V.showEmail(row)},
};

function rowEl(g, row) {
  const li = h("li", {class: "bf-row" + (row.unread ? " unread" : "")});
  const sender = row.sender || row.to_name || row.from || "Unknown sender";
  li.append(h("div", {class: "bf-row-top"},
    row.unread ? h("span", {class: "bf-dot", title: "Unread"}, h("span", {class: "bf-sr"}, "Unread")) : null,
    h("span", {class: "bf-sender", text: g.key === "follow_ups" ? `To ${sender}` : sender}),
    row.n > 1 ? h("span", {class: "bf-n", title: plural(num(row.n), "email"), text: `×${row.n}`}) : null,
    row.days_waiting != null ? h("span", {class: "bf-wait", text: `waiting ${plural(num(row.days_waiting), "day")}`}) : null,
    h("span", {class: "bf-date", title: row.date || null, text: rowDate(row.date)})));
  li.append(h("div", {class: "bf-subj", text: row.subject || "(no subject)"}));
  if (row.snippet) li.append(h("div", {class: "bf-snip", title: row.snippet, text: row.snippet}));
  const acts = h("div", {class: "bf-acts"});
  for (const a of row.actions || []) {
    const spec = ACTIONS[a];
    if (!spec) continue;
    const b = V.btn(spec.label, () => { if (!V.busy) spec.run(row, li); }, "bf-pill" + (spec.quiet ? " bf-quiet" : ""),
      a === "open" ? null : {"data-turn": true});
    if (a !== "open" && V.busy) b.disabled = true;
    acts.append(b);
  }
  if (acts.childNodes.length) li.append(acts);
  return li;
}

// ---------------------------------------------------------------- groups (accordion)

function groupBody(g, inner) {
  const rows = Array.isArray(g.rows) ? g.rows : [];
  const empty = () => inner.append(h("p", {class: "bf-empty", text: g.empty_text || "Nothing here."}));
  if (isPromo(g)) {
    const msgs = shownCount(g);
    if (!msgs) return empty();
    inner.append(h("div", {class: "bf-promo"},
      h("span", {text: `${plural(msgs, "promotional email")} from ${plural(entries(g) || msgs, "sender")}`}),
      V.btn("Archive all", () => { if (!V.busy) V.doAction({action: "archive_all"}, `Archive all ${plural(msgs, "promotion")}`); },
        "bf-pill", {"data-turn": true, disabled: V.busy})));
    return;
  }
  if (!rows.length) return empty();
  const ul = h("ul", {class: "bf-rows"});
  rows.forEach((row, i) => { const li = rowEl(g, row); if (i >= ROW_CAP) li.hidden = true; ul.append(li); });
  inner.append(ul);
  if (rows.length > ROW_CAP) {
    const more = V.btn(`Show ${rows.length - ROW_CAP} more`, () => {
      const first = ul.querySelector("li[hidden]");
      ul.querySelectorAll("li[hidden]").forEach(li => { li.hidden = false; });
      more.remove();
      const target = first && first.querySelector("button");
      if (target) target.focus();
    }, "bf-link bf-more");
    inner.append(more);
  }
  const unlisted = entries(g) - rows.length;
  if (unlisted > 0) inner.append(h("p", {class: "bf-unlisted", text: `${unlisted} more not shown`}));
}

function groupEl(root, g) {
  const key = String(g.key || ""), n = shownCount(g), id = `bf${++seq}`;
  const saved = root._open[key];
  const open = n > 0 && (saved != null ? saved : OPEN_BY_DEFAULT.has(key));
  const sec = h("section", {class: `bf-group g-${key}` + (open ? " open" : "") + (n ? "" : " is-empty")});
  const head = h("button", {type: "button", class: "bf-head", id: `${id}h`, "aria-expanded": String(open), "aria-controls": `${id}p`},
    h("span", {class: "bf-count", text: String(n)}),
    h("span", {class: "bf-label", text: g.title || key}),
    h("span", {class: "bf-chev", "aria-hidden": "true"}));
  head.addEventListener("click", () => {
    const now = !sec.classList.contains("open");
    sec.classList.toggle("open", now);
    head.setAttribute("aria-expanded", String(now));
    root._open[key] = now;
  });
  const inner = h("div", {class: "bf-inner"});
  groupBody(g, inner);
  sec.append(head, h("div", {class: "bf-panel", id: `${id}p`, role: "region", "aria-labelledby": `${id}h`}, inner));
  return sec;
}

// ---------------------------------------------------------------- the page

function updateStamp(root) {
  const b = root._bf || {}, mins = ageMin(b), parts = [];
  if (mins != null) parts.push(mins < 1 ? "Built just now" : mins < 60 ? `Built ${mins} min ago` : `Built ${Math.floor(mins / 60)} h ago`);
  if (b.covers_days != null) parts.push(`covers ${plural(num(b.covers_days), "day")} of mail`);
  root._stamp.textContent = parts.join(" · ") + (parts.length ? " · " : "");
  const stale = mins != null && mins >= STALE_MIN;
  root.classList.toggle("stale", stale);
  root._stamp.title = stale ? "Older than 30 minutes: refresh to see new mail" : "";
}

function fill(root, b) {
  root.textContent = "";
  root._bf = b;
  root.append(h("div", {class: "bf-greet"},
    h("h2", {class: "brief-greeting", text: greetingText(b)}),
    h("span", {class: "brief-voice-slot"})));
  const date = todayLabel(), line = !b.error && b.built_at ? todayText(b) : "";
  root.append(h("div", {class: "bf-today"},
    h("p", {class: "bf-kicker"}, "Today", date ? h("span", {class: "bf-kicker-date", text: ` · ${date}`}) : null),
    b.error ? h("p", {class: "bf-err", role: "alert", text: `Could not build the brief: ${b.error}`})
      : line ? h("p", {class: "bf-today-line", text: line}) : null));
  root._stamp = h("span", {class: "bf-stamp-text"});
  root._refreshBtn = V.btn("Refresh", () => refresh(root), "bf-link bf-refresh");
  root._stampRow = h("p", {class: "bf-stamp"}, root._stamp, root._refreshBtn);
  root.append(h("div", {class: "bf-rule"}), root._stampRow);
  const groups = (b.groups || []).filter(Boolean);
  if (!b.error) {
    if (!groups.some(g => shownCount(g) > 0)) root.append(h("p", {class: "bf-empty-all", text: "Nothing needs your attention right now."}));
    else root.append(h("div", {class: "bf-groups"}, groups.map(g => groupEl(root, g))));
  }
  updateStamp(root);
}

// Re-fetch like the old card's Refresh, but with the chat's session so the server's copy of the
// brief (which Reply and Archive all read) matches what is on screen. Re-renders in place.
async function refresh(root) {
  if (root._refreshing) return;
  root._refreshing = true;
  const hadFocus = document.activeElement === root._refreshBtn;
  root._refreshBtn.disabled = true;
  if (root._refreshErr) { root._refreshErr.remove(); root._refreshErr = null; }
  root._stamp.textContent = "Refreshing from Gmail… ";
  const sid = V.sessionId;
  try {
    const b = await V.api(`/brief?refresh=1${sid ? `&session_id=${encodeURIComponent(sid)}` : ""}`);
    fill(root, b || {});
    if (hadFocus) root._refreshBtn.focus();
    V.emit("brief:shown", {brief: b || {}, el: root});
  } catch (e) {
    updateStamp(root);
    root._refreshErr = h("p", {class: "bf-err", role: "alert", text: `Refresh failed: ${e.message}`});
    root._stampRow.after(root._refreshErr);
    root._refreshBtn.disabled = false;
  } finally { root._refreshing = false; }
}

V.renderBrief = brief => {
  const root = h("section", {class: "bf-brief", "aria-label": "Inbox brief", tabindex: "-1"});
  root._open = {};
  root.refresh = () => refresh(root);
  root.isStale = () => { const m = ageMin(root._bf); return m != null && m >= STALE_MIN; };
  fill(root, brief || {});
  return root;
};

setInterval(() => document.querySelectorAll(".bf-brief").forEach(el => {
  if (el._bf && el._stamp && !el._refreshing) updateStamp(el);
}), 30000);
})();
