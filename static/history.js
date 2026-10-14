/* history.js: the rail's History section, earlier chats shown read-only, and the small
 * actions under each new reply (Copy, Regenerate, Helpful / Not helpful).
 *
 * Server: GET /chats, GET / PATCH / DELETE /chats/{id}, POST /feedback (assistant/routes_history.py).
 * Opening an earlier chat moves the live thread aside (kept, not thrown away) and disables the
 * composer; the live chat's row, any new turn, or "Start a new chat" brings the composer back.
 * DOM via createElement / textContent only. Contract: .build/CONTRACT-v2.md.
 */
"use strict";
(() => {
  const V = window.Vaani;
  const $ = s => document.querySelector(s);
  const SVG = "http://www.w3.org/2000/svg";

  let bodyEl = null;      // the rail card body SHELL handed to render()
  let viewing = null;     // {id, title} of the earlier chat on screen, or null
  let stash = null;       // the live thread's nodes while an earlier chat is on screen
  let menuOpen = null;    // {menu, more} for the open "⋯" menu
  let renaming = false;   // an inline rename is in progress: do not redraw under it
  let placeholder = null; // the composer's placeholder before read-only mode
  let timer = null;

  const toast = (text, opts) => {
    if (typeof V.toast === "function") V.toast(text, opts || {});
    else if (opts && opts.tone) console.warn(text);
  };

  // ---------------------------------------------------------------- icons (stroke, 24 px grid)
  const ICONS = {
    copy: ["M9 9h10a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2h-8a2 2 0 0 1-2-2z", "M15 9V5a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h4"],
    redo: ["M3 12a9 9 0 1 0 2.6-6.4L3 8", "M3 3v5h5"],
    thumb: ["M7 10v11H4a1 1 0 0 1-1-1v-9a1 1 0 0 1 1-1z",
      "M7 10l4-7a2.2 2.2 0 0 1 3.2 2.4L13 10h6a2 2 0 0 1 2 2.3l-1.3 7A2 2 0 0 1 17.7 21H7"],
    check: ["M5 12.5l4.5 4.5L19 7.5"],
  };
  function icon(name, flip) {
    const s = document.createElementNS(SVG, "svg");
    for (const [k, v] of Object.entries({viewBox: "0 0 24 24", width: "16", height: "16", fill: "none", stroke: "currentColor",
      "stroke-width": "1.7", "stroke-linecap": "round", "stroke-linejoin": "round", "aria-hidden": "true", focusable: "false"})) s.setAttribute(k, v);
    const g = document.createElementNS(SVG, "g");
    if (flip) g.setAttribute("transform", "rotate(180 12 12)");
    for (const d of ICONS[name]) { const p = document.createElementNS(SVG, "path"); p.setAttribute("d", d); g.append(p); }
    s.append(g);
    return s;
  }
  function iconBtn(name, label, flip) {
    const b = V.h("button", {type: "button", class: "msg-act", "aria-label": label, title: label});
    b.append(icon(name, flip));
    return b;
  }

  // ---------------------------------------------------------------- rail section

  V.rail.register({
    id: "history", title: "History", icon: "history", order: 40,
    render(body) { bodyEl = body; schedule(0); },
  });

  function schedule(ms) { clearTimeout(timer); timer = setTimeout(load, ms == null ? 300 : ms); }

  async function load() {
    if (!bodyEl || typeof V.api !== "function") return;  // before "ready": the helpers do not exist yet
    try { draw(await V.api("/chats")); } catch (e) { draw(null, e); }
  }

  function draw(j, err) {
    if (!bodyEl || renaming) return;
    closeMenu();
    bodyEl.textContent = "";
    if (err) {
      bodyEl.append(V.h("p", {class: "history-empty", text: err.status === 404 ? "History is not available on this server." : `Could not load history: ${err.message}`}));
      return;
    }
    const groups = ((j && j.groups) || []).filter(g => g && (g.chats || []).length);
    if (!groups.length) { bodyEl.append(V.h("p", {class: "history-empty", text: "Your chats will show up here."})); return; }
    const current = viewing ? viewing.id : V.sessionId;
    for (const g of groups) {
      const ul = V.h("ul", {class: "history-list"});
      for (const c of g.chats) ul.append(row(c, c.id === current));
      bodyEl.append(V.h("div", {class: "history-group"}, V.h("div", {class: "history-label", text: g.label}), ul));
    }
  }

  function row(c, current) {
    const title = c.title || "Untitled chat";
    const li = V.h("li", {class: "history-row" + (current ? " current" : ""), "data-id": c.id});
    const open = V.h("button", {type: "button", class: "history-open", title, "aria-current": current ? "true" : null, text: title});
    open.onclick = () => openChat(Object.assign({}, c, {title}));
    const more = V.h("button", {type: "button", class: "history-more", "aria-label": `More options for ${title}`,
      "aria-haspopup": "menu", "aria-expanded": "false", text: "⋯"});
    more.onclick = e => { e.stopPropagation(); toggleMenu(li, more, Object.assign({}, c, {title})); };
    li.append(open, more);
    return li;
  }

  function highlight() {
    if (!bodyEl) return;
    const current = viewing ? viewing.id : V.sessionId;
    for (const li of bodyEl.querySelectorAll(".history-row")) {
      const on = li.dataset.id === current;
      li.classList.toggle("current", on);
      const b = li.querySelector(".history-open");
      if (b && on) b.setAttribute("aria-current", "true"); else if (b) b.removeAttribute("aria-current");
    }
  }

  // ---------------------------------------------------------------- "⋯" menu: Rename, Delete

  function closeMenu() {
    if (!menuOpen) return;
    menuOpen.menu.remove();
    menuOpen.more.setAttribute("aria-expanded", "false");
    menuOpen = null;
  }

  function toggleMenu(li, more, c) {
    const same = menuOpen && menuOpen.more === more;
    closeMenu();
    if (same) return;
    const rename = V.h("button", {type: "button", role: "menuitem", text: "Rename"});
    const del = V.h("button", {type: "button", role: "menuitem", class: "risk", text: "Delete"});
    const menu = V.h("div", {class: "history-menu", role: "menu", "aria-label": `Options for ${c.title}`}, rename, del);
    rename.onclick = e => { e.stopPropagation(); closeMenu(); startRename(li, c); };
    del.onclick = e => { e.stopPropagation(); closeMenu(); remove(c); };
    menu.addEventListener("keydown", e => {
      if (e.key === "ArrowDown" || e.key === "ArrowUp") { e.preventDefault(); (document.activeElement === rename ? del : rename).focus(); }
    });
    li.append(menu);
    more.setAttribute("aria-expanded", "true");
    menuOpen = {menu, more};
    rename.focus();
  }

  document.addEventListener("click", e => {
    if (menuOpen && !menuOpen.menu.contains(e.target) && e.target !== menuOpen.more) closeMenu();
  });
  // Escape closes the menu first (capture: before app.js closes the whole rail).
  document.addEventListener("keydown", e => {
    if (e.key !== "Escape" || !menuOpen) return;
    e.stopPropagation();
    const more = menuOpen.more;
    closeMenu();
    more.focus();
  }, true);

  function startRename(li, c) {
    const open = li.querySelector(".history-open");
    if (!open) return;
    const input = V.h("input", {type: "text", class: "history-rename", maxlength: "80", "aria-label": "Chat title"});
    input.value = c.title;
    renaming = true;
    let done = false;
    const finish = async save => {
      if (done) return;
      done = true;
      const t = input.value.trim();
      if (save && t && t !== c.title) {
        try {
          const j = await V.api(`/chats/${encodeURIComponent(c.id)}`, {title: t}, "PATCH");
          if (viewing && viewing.id === c.id) setBannerTitle(j.title || t);
        } catch (e) { toast(`Could not rename: ${e.message}`, {tone: "risk"}); }
      }
      renaming = false;
      schedule(0);
    };
    input.addEventListener("keydown", e => {
      if (e.key === "Enter") { e.preventDefault(); finish(true); }
      else if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); finish(false); }
    });
    input.addEventListener("blur", () => finish(false));
    open.replaceWith(input);
    input.focus();
    input.select();
  }

  async function remove(c) {
    const ok = await V.confirmDialog({title: "Delete this chat?", text: `"${c.title}" will be removed from your history.`,
      ok: "Delete", cancel: "Keep it"});
    if (!ok) return;
    try { await V.api(`/chats/${encodeURIComponent(c.id)}`, undefined, "DELETE"); }
    catch (e) { toast(`Could not delete: ${e.message}`, {tone: "risk"}); return; }
    toast("Chat deleted");
    if ((viewing && viewing.id === c.id) || c.id === V.sessionId) V.newChat();
    schedule(0);
  }

  // ---------------------------------------------------------------- an earlier chat, read-only

  async function openChat(c) {
    closeMenu();
    if (c.id === V.sessionId) { leaveView(); return; }  // the live chat: just come back to it
    if (viewing && viewing.id === c.id) return;
    if (V.busy) { toast("Wait for the current reply to finish first."); return; }
    let j;
    try { j = await V.api(`/chats/${encodeURIComponent(c.id)}`); }
    catch (e) { toast(`Could not open that chat: ${e.message}`, {tone: "risk"}); if (e.status === 404) schedule(0); return; }
    show(j);
    // Narrow screens: the rail covers the thread, so close it the way its own items do.
    if (document.querySelector(".rail-open") && window.matchMedia("(max-width: 899px)").matches) {
      const menu = $("#menuBtn");
      if (menu) menu.click();
    }
  }

  const SAID = {confirm: "Approved", cancel: "Skipped"};
  const flat = s => String(s == null ? "" : s).replace(/\*\*/g, "").replace(/\s+/g, " ").trim();
  const actionLabel = a => { const t = flat(String(a || "action").replace(/_/g, " ")); return t.charAt(0).toUpperCase() + t.slice(1); };

  function summary(label, text) {
    const t = flat(text);
    return V.h("div", {class: "history-card", title: t},
      V.h("span", {class: "history-card-label", text: label}), V.h("span", {class: "history-card-text", text: t}));
  }

  function receiptLine(rc) {
    const first = Array.isArray(rc.report) && rc.report.length ? rc.report[0] : "";
    return first || rc.status || "Google answered";
  }

  function drawItem(view, it) {
    const req = it.request || {}, res = it.response || {};
    if (it.unreadable) { view.append(summary("Turn", "This turn cannot be read with the current key.")); return; }
    if (it.kind === "new") {
      const b = res.brief;
      if (b) view.append(summary("Brief", typeof b.greeting === "string" && b.greeting ? b.greeting : "Your brief"));
      return;
    }
    const said = it.kind === "chat" ? req.text : it.kind === "action" ? actionLabel(req.action) : SAID[it.kind];
    if (said) view.append(V.h("div", {class: "msg user"}, String(said)));
    if (res.reply) { const b = V.h("div", {class: "msg bot"}); V.renderReply(b, res.reply); view.append(b); }
    const d = res.pending_draft;
    if (d) view.append(summary("Card", (d.card && d.card.title) || (d.summary ? `Draft: ${d.summary}` : "A card waited for approval")));
    if (res.receipt) view.append(summary("Receipt", receiptLine(res.receipt)));
    if (res.offer) view.append(summary("Offer", res.offer.title || res.offer.text || "An autonomy offer"));
  }

  function setBannerTitle(t) {
    const el = document.querySelector(".history-banner-title");
    if (el) el.textContent = t;
    if (viewing) viewing.title = t;
  }

  function show(j) {
    const log = $("#log");
    if (!log) return;
    if (!viewing) {  // keep the live thread, untouched, for when the user comes back to it
      stash = document.createDocumentFragment();
      while (log.firstChild) stash.append(log.firstChild);
    } else log.textContent = "";
    viewing = {id: j.id, title: j.title};
    const view = V.h("div", {class: "history-view"});
    view.append(V.h("div", {class: "history-banner", role: "note"},
      V.h("span", {class: "history-banner-text"}, "Earlier chat · ", V.h("strong", {class: "history-banner-title", text: j.title}), " · read-only"),
      V.btn("Start a new chat", () => V.newChat(), "primary sm")));
    const items = j.items || [];
    for (const it of items) drawItem(view, it);
    if (!items.some(it => it.kind !== "new")) view.append(V.h("p", {class: "history-empty", text: "Nothing was said in this chat."}));
    log.append(view);
    setComposer(false);
    const sc = $("#scroller");
    if (sc) sc.scrollTop = 0;
    highlight();
  }

  function leaveView() {
    if (!viewing) return;
    const log = $("#log");
    const view = log && log.querySelector(".history-view");
    if (view) view.remove();
    if (log && stash) log.insertBefore(stash, log.firstChild);
    stash = null;
    viewing = null;
    setComposer(true);
    highlight();
  }

  function setComposer(on) {
    const box = $("#box"), send = $("#send"), form = $("#f");
    if (form) form.classList.toggle("history-readonly", !on);
    if (box) {
      if (!on) {
        if (placeholder == null) placeholder = box.placeholder;
        box.placeholder = "Read-only: start a new chat to continue";
        box.disabled = true;
      } else {
        if (placeholder != null) box.placeholder = placeholder;
        placeholder = null;
        box.disabled = !!V.busy;
      }
    }
    if (send) send.disabled = on ? !!V.busy : true;
  }

  // Nothing typed while an earlier chat is on screen may reach the live session.
  document.addEventListener("submit", e => {
    if (viewing && e.target && e.target.id === "f") { e.preventDefault(); e.stopImmediatePropagation(); }
  }, true);

  // ---------------------------------------------------------------- reply actions

  async function copyText(text) {
    try {
      if (navigator.clipboard && window.isSecureContext) { await navigator.clipboard.writeText(text); return true; }
    } catch (_) { /* fall back below */ }
    const ta = V.h("textarea", {readonly: true, "aria-hidden": "true", style: "position:fixed;top:0;left:0;opacity:0"});
    ta.value = text;
    document.body.append(ta);
    ta.select();
    let ok = false;
    try { ok = document.execCommand("copy"); } catch (_) { ok = false; }
    ta.remove();
    return ok;
  }

  function actionsFor(el, r, path, payload) {
    const sid = V.sessionId || "";
    const bar = V.h("div", {class: "msg-actions", role: "group", "aria-label": "Reply actions"});
    const copy = iconBtn("copy", "Copy reply");
    copy.onclick = async () => {
      const ok = await copyText((el.innerText || r.reply || "").trim());
      copy.replaceChildren(icon(ok ? "check" : "copy"));
      copy.setAttribute("aria-label", ok ? "Copied" : "Copy failed");
      toast(ok ? "Copied" : "Could not copy", ok ? {tone: "ok", ms: 1500} : {tone: "warn"});
      setTimeout(() => { copy.replaceChildren(icon("copy")); copy.setAttribute("aria-label", "Copy reply"); }, 1500);
    };
    bar.append(copy);
    const asked = payload && typeof payload.text === "string" ? payload.text.trim() : "";
    if (path === "/chat" && asked && !r.pending_draft && !r.receipt) {
      const again = iconBtn("redo", "Regenerate reply");
      again.setAttribute("data-turn", "");  // app.js setBusy() disables it while a turn runs
      again.onclick = () => { if (!V.busy) V.submit(asked, {via: "typed"}); };
      bar.append(again);
    }
    const up = iconBtn("thumb", "Helpful"), down = iconBtn("thumb", "Not helpful", true);
    up.setAttribute("aria-pressed", "false");
    down.setAttribute("aria-pressed", "false");
    const vote = async (value, b, other) => {
      if (b.getAttribute("aria-pressed") === "true") return;
      const before = other.getAttribute("aria-pressed");
      b.setAttribute("aria-pressed", "true");
      other.setAttribute("aria-pressed", "false");
      try {
        await V.api("/feedback", {session_id: sid, value, excerpt: String(r.reply || "").slice(0, 200)});
        toast("Thanks for the feedback", {ms: 1800});
      } catch (e) {
        b.setAttribute("aria-pressed", "false");
        other.setAttribute("aria-pressed", before);
        toast(`Could not save feedback: ${e.message}`, {tone: "warn"});
      }
    };
    up.onclick = () => vote("up", up, down);
    down.onclick = () => vote("down", down, up);
    bar.append(up, down);
    return bar;
  }

  // ---------------------------------------------------------------- events

  V.on("ready", () => schedule(0));
  V.on("chat:new", () => {
    viewing = null;  // newChat() already cleared the thread, banner included
    stash = null;
    setComposer(true);
    schedule();
  });
  V.on("turn:start", () => { if (viewing) leaveView(); });  // a chip or voice turn: back to the live chat
  V.on("turn:result", ({r, path, payload} = {}) => {
    schedule();
    if (!r || !r.reply || viewing) return;
    const log = $("#log");
    const bots = log ? log.querySelectorAll(".msg.bot:not(.err)") : [];
    const el = bots[bots.length - 1];
    if (!el || el.dataset.actions) return;
    el.dataset.actions = "1";
    el.after(actionsFor(el, r, path, payload));
  });
})();
