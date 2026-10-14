/* rail.js: Vaani's own left-rail sections (owner: RAIL). Contract: .build/CONTRACT-v2.md.
 *
 * Three sections, registered on "ready": Today's meetings (GET /meetings/today), Follow-ups
 * (GET /followups; a click drafts a nudge exactly like the old Follow-ups panel) and Drafts saved
 * (GET /drafts/recent: Vaani never sends mail, so the drafts it saved are its "Sent emails").
 * SHELL renders each section as a card from load() -> {count, items: [{title, sub, meta, onClick}], empty}
 * and refreshes it on ready, chat:new and turn:result. Also exposes Vaani.icons: small line icons
 * (stroke currentColor) as functions that return a new <svg> element each call.
 */
"use strict";
(() => {
  const V = window.Vaani;
  const NS = "http://www.w3.org/2000/svg";

  // ---- icons: 20x20 line drawings, one path list each
  const PATHS = {
    calendar: ["M5 4.5h10a2 2 0 0 1 2 2V15a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V6.5a2 2 0 0 1 2-2Z", "M3 8.5h14", "M7 2.75v3.5", "M13 2.75v3.5"],
    bell: ["M5.25 13.75V8.75a4.75 4.75 0 0 1 9.5 0v5l1.5 1.5H3.75Z", "M8.25 17.25a1.9 1.9 0 0 0 3.5 0"],
    draft: ["M12.75 3.75l3.5 3.5L8 15.5H4.5V12Z", "M11 5.5l3.5 3.5", "M10.5 17.25h5.75"],
    history: ["M3.75 10a6.25 6.25 0 1 0 1.83-4.42", "M3.5 3.5v2.75h2.75", "M10 6.75V10l2.25 1.5"],
  };
  function icon(name) {
    const svg = document.createElementNS(NS, "svg");
    for (const [k, v] of Object.entries({viewBox: "0 0 20 20", width: "16", height: "16", fill: "none", stroke: "currentColor",
      "stroke-width": "1.5", "stroke-linecap": "round", "stroke-linejoin": "round", "aria-hidden": "true", focusable: "false"})) {
      svg.setAttribute(k, v);
    }
    svg.setAttribute("class", `vi vi-${name}`);
    for (const d of PATHS[name] || []) {
      const p = document.createElementNS(NS, "path");
      p.setAttribute("d", d);
      svg.append(p);
    }
    return svg;
  }
  V.icons = Object.assign(V.icons || {}, {
    calendar: () => icon("calendar"), bell: () => icon("bell"), draft: () => icon("draft"), history: () => icon("history"),
  });

  // ---- helpers
  const safeLink = link => (typeof link === "string" && /^https:\/\//i.test(link) ? link : null);
  const opener = link => (safeLink(link) ? () => { window.open(safeLink(link), "_blank", "noopener,noreferrer"); } : undefined);
  const hhmm = iso => (/T(\d{2}:\d{2})/.exec(String(iso || "")) || [])[1] || "";
  function timeRange(ev) {
    if (ev.all_day) return "All day";
    const a = hhmm(ev.start), b = hhmm(ev.end);
    return a && b ? `${a}–${b}` : a;
  }
  function tzOpt() { const me = V.me; return me && me.tz ? {timeZone: me.tz} : {}; }
  function savedAt(iso) {  // "14:05" today, "Tue 18:30" otherwise (the window is 24 h)
    const d = new Date(iso);
    if (isNaN(d)) return "";
    try {
      const day = x => x.toLocaleDateString("en-GB", tzOpt());
      const t = d.toLocaleTimeString("en-GB", Object.assign({hour: "2-digit", minute: "2-digit"}, tzOpt()));
      return day(d) === day(new Date()) ? t : `${d.toLocaleDateString("en-GB", Object.assign({weekday: "short"}, tzOpt()))} ${t}`;
    } catch (_) { return ""; }
  }

  // ---- sections
  async function loadMeetings() {
    const j = await V.api("/meetings/today");  // a failed request throws: SHELL shows its error line
    const events = Array.isArray(j && j.events) ? j.events : [];
    return {
      count: events.length,
      empty: (j && j.error) || "Nothing on your calendar today.",
      items: events.map(ev => ({title: ev.title || "(no title)", sub: ev.where || "", meta: timeRange(ev), onClick: opener(ev.link)})),
    };
  }

  async function loadFollowups() {
    const j = await V.api("/followups");
    const rows = Array.isArray(j && j.rows) ? j.rows : [];
    return {
      count: rows.length,
      empty: "No one is waiting on you.",
      items: rows.map(row => {
        const name = row.to_name || row.sender || row.to_email || "them";
        return {
          title: name,
          sub: row.days_waiting != null ? `waiting ${V.plural(row.days_waiting, "day")}` : (row.subject || ""),
          // Same call as the old Follow-ups panel's "Draft nudge" button.
          onClick: () => { if (!V.busy) V.doAction({action: "nudge", message_id: row.message_id || row.id, thread_id: row.thread_id}, `Draft a nudge to ${name}`); },
        };
      }),
    };
  }

  async function loadDrafts() {
    const j = await V.api("/drafts/recent");
    const drafts = Array.isArray(j && j.drafts) ? j.drafts : [];
    return {
      count: drafts.length,
      empty: (j && j.error) || "No drafts saved in the last 24 hours.",
      items: drafts.map(d => ({title: d.title || "Email draft", sub: d.to ? `to ${d.to}` : "", meta: savedAt(d.when), onClick: opener(d.link)})),
    };
  }

  V.on("ready", ({me} = {}) => {
    V.rail.register({id: "meetings", title: "Today's meetings", icon: "calendar", order: 10, load: loadMeetings});
    // Follow-ups stay off when the server turns them off (VAANI_FOLLOWUPS), as the old panel did.
    if (!(me && me.flags && me.flags.followups === false)) {
      V.rail.register({id: "followups", title: "Follow-ups", icon: "bell", order: 20, load: loadFollowups});
    }
    V.rail.register({id: "drafts", title: "Drafts saved", icon: "draft", order: 30, load: loadDrafts});
  });
})();
