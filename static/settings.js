/* Settings: full pages that cover the chat column (.center) while open; the rail stays.
 *
 * Vaani.openSettings(id?) shows a registered page (the first by order when no id is given) and
 * Vaani.closeSettings() returns to the chat. Pages come from Vaani.settings.pages:
 * {id, title, icon, order, lede?, render(container)}; render may return a cleanup function,
 * called when the page is left. Routing is #/settings/<id>: Esc, the back link and the browser
 * Back button all return to the chat. Built-in pages: Autonomy (10), Proof (20), Account (30).
 * DOM is built with createElement / textContent only. Contract: .build/CONTRACT-v2.md.
 */
"use strict";
(() => {
const V = window.Vaani;
if (!V) return;
const HASH = /^#\/settings(?:\/([\w-]+))?\/?$/;
const SVG = "http://www.w3.org/2000/svg";
const h = (...a) => V.h(...a);

let view = null, navEl = null, iconEl = null, titleEl = null, ledeEl = null, pageEl = null;
let currentId = null, wantedId = null, returnFocus = null, cleanup = null, pendingOpen = null;

// ---------------------------------------------------------------- icons
// 24px line icons by name. A page's icon may also be a Node, a function returning one, an <svg> string or an emoji.
const ICONS = {
  autonomy: ["M4 7h9", "M17 7h3", "M4 17h3", "M11 17h9", "M13 7a2 2 0 1 0 4 0a2 2 0 1 0-4 0", "M7 17a2 2 0 1 0 4 0a2 2 0 1 0-4 0"],
  proof: ["M12 3l7 3v5c0 4.5-3 8.3-7 10c-4-1.7-7-5.5-7-10V6z", "M8.5 12l2.5 2.5l4.5-4.5"],
  account: ["M12 12a4 4 0 1 0 0-8a4 4 0 1 0 0 8z", "M4.5 20c1.4-3.3 4.3-5 7.5-5s6.1 1.7 7.5 5"],
  voice: ["M12 3a3 3 0 0 0-3 3v6a3 3 0 0 0 6 0V6a3 3 0 0 0-3-3z", "M5.5 11a6.5 6.5 0 0 0 13 0", "M12 17.5V21"],
  settings: ["M12 15a3 3 0 1 0 0-6a3 3 0 1 0 0 6z", "M12 2.5v3M12 18.5v3M2.5 12h3M18.5 12h3M5.3 5.3l2.1 2.1M16.6 16.6l2.1 2.1M5.3 18.7l2.1-2.1M16.6 7.4l2.1-2.1"],
};
Object.assign(ICONS, {sliders: ICONS.autonomy, shield: ICONS.proof, user: ICONS.account, mic: ICONS.voice, gear: ICONS.settings});

function svgIcon(paths) {
  const s = document.createElementNS(SVG, "svg");
  for (const [k, v] of [["viewBox", "0 0 24 24"], ["width", "20"], ["height", "20"], ["aria-hidden", "true"], ["focusable", "false"]]) s.setAttribute(k, v);
  for (const d of paths) {
    const p = document.createElementNS(SVG, "path");
    for (const [k, v] of [["d", d], ["fill", "none"], ["stroke", "currentColor"], ["stroke-width", "1.6"], ["stroke-linecap", "round"], ["stroke-linejoin", "round"]]) p.setAttribute(k, v);
    s.append(p);
  }
  return s;
}

function iconFor(page) {
  const ic = page.icon;
  if (ic instanceof Node) return ic.cloneNode(true);
  if (typeof ic === "function") { try { const n = ic(); if (n instanceof Node) return n; } catch (_) { /* fall back below */ } }
  const s = typeof ic === "string" ? ic.trim() : "";
  if (ICONS[s]) return svgIcon(ICONS[s]);
  if (/^<svg[\s>]/i.test(s)) {
    // Parsed as XML (never innerHTML); scripts, foreignObject and on* attributes are refused.
    try {
      const doc = new DOMParser().parseFromString(s, "image/svg+xml");
      const root = doc.documentElement;
      if (root.nodeName.toLowerCase() === "svg" && !doc.querySelector("parsererror, script, foreignObject")) {
        for (const n of [root, ...root.querySelectorAll("*")])
          for (const a of [...n.attributes]) if (/^on/i.test(a.name) || /^\s*javascript:/i.test(a.value)) n.removeAttribute(a.name);
        root.setAttribute("aria-hidden", "true");
        return document.importNode(root, true);
      }
    } catch (_) { /* fall back below */ }
  }
  if (s && s.length <= 4) return h("span", {class: "sv-glyph", "aria-hidden": "true", text: s});
  return svgIcon(ICONS[page.id] || ICONS.settings);
}

// ---------------------------------------------------------------- view

function build() {
  if (view) return true;
  const center = document.querySelector(".center");
  if (!center) return false;
  const back = h("button", {type: "button", class: "sv-back", onclick: () => closeSettings()},
    h("span", {"aria-hidden": "true", text: "‹"}), " back to chat");
  navEl = h("nav", {class: "sv-nav", "aria-label": "Settings pages"});
  iconEl = h("span", {class: "sv-icon"});
  titleEl = h("h1", {id: "svTitle", tabindex: "-1"});
  ledeEl = h("p", {class: "sv-lede"});
  pageEl = h("div", {class: "sv-body"});
  view = h("section", {id: "settingsView", class: "sv", "aria-labelledby": "svTitle", hidden: true},
    h("div", {class: "sv-col"}, back, navEl, h("header", {class: "sv-head"}, iconEl, titleEl), ledeEl, pageEl));
  center.append(view);
  return true;
}

function renderNav() {
  const had = navEl.contains(document.activeElement) ? document.activeElement.dataset.page : null;
  navEl.textContent = "";
  const pages = V.settings.pages;
  navEl.hidden = pages.length < 2;
  for (const p of pages) {
    const a = h("a", {href: `#/settings/${p.id}`, class: "sv-pill", "data-page": p.id, "aria-current": p.id === currentId ? "page" : null},
      iconFor(p), h("span", {text: p.title || p.id}));
    a.addEventListener("click", e => {
      if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button) return;  // a new tab or window keeps the link
      e.preventDefault();
      openSettings(p.id);
    });
    navEl.append(a);
  }
  const f = had && [...navEl.children].find(n => n.dataset.page === had);
  if (f) f.focus();
}

function markNav() {
  for (const a of navEl.children) {
    if (a.dataset.page === currentId) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  }
}

function teardown() {
  const c = cleanup;
  cleanup = null;
  if (c) { try { c(); } catch (e) { console.error("Settings page cleanup failed", e); } }
}

function renderPage(p) {
  teardown();
  iconEl.textContent = "";
  iconEl.append(iconFor(p));
  titleEl.textContent = p.title || p.id;
  const lede = p.lede || p.description || "";
  ledeEl.textContent = lede;
  ledeEl.hidden = !lede;
  pageEl.textContent = "";
  // A fresh container per render: late callbacks from the previous page land in a detached node.
  const box = pageEl.appendChild(h("div", {class: "sv-page", "data-page": p.id}));
  const fail = e => {
    console.error(`Settings page ${p.id} failed`, e);
    box.append(h("p", {class: "inline-err", role: "alert", text: `Could not show ${p.title || p.id}: ${(e && e.message) || e}`}));
  };
  try {
    const r = typeof p.render === "function" ? p.render(box) : null;
    if (typeof r === "function") cleanup = r;
    else if (r && typeof r.then === "function") r.catch(fail);
  } catch (e) { fail(e); }
  view.scrollTop = 0;
}

// The mobile rail and the old drawer are overlays; settings replaces both.
function closeOverlays() {
  const app = document.getElementById("app"), drawer = document.getElementById("drawer");
  const railOpen = app && app.classList.contains("rail-open"), drawerOpen = drawer && !drawer.hidden;
  if (!railOpen && !drawerOpen) return;
  const scrim = document.getElementById("scrim"), close = document.getElementById("drawerClose");
  if (scrim) scrim.click(); else if (drawerOpen && close) close.click();
}

function show(id) {
  if (!build()) return false;
  const pages = V.settings.pages;
  let p = pages.find(x => x.id === id);
  wantedId = !p && id ? id : null;  // not registered yet: show the first page, switch when it registers
  if (!p) p = pages[0];
  if (!p) return false;
  const opening = view.hidden, changed = p.id !== currentId;
  currentId = p.id;
  if (opening) {
    returnFocus = document.activeElement;
    closeOverlays();
    view.hidden = false;
    view.parentNode.classList.add("sv-open");
    document.body.classList.add("settings-open");
    renderNav();
  } else markNav();
  if (opening || changed) renderPage(p);
  if (opening) titleEl.focus({preventScroll: true});
  V.emit("settings:open", {id: p.id});
  return true;
}

function hide() {
  if (!view || view.hidden) return;
  teardown();
  view.hidden = true;
  view.parentNode.classList.remove("sv-open");
  document.body.classList.remove("settings-open");
  pageEl.textContent = "";
  currentId = null;
  wantedId = null;
  V.emit("settings:close", {});
  const t = returnFocus;
  returnFocus = null;
  const r = t && t.isConnected && t !== document.body ? t.getBoundingClientRect() : null;
  if (r && r.width && r.right > 0 && r.left < window.innerWidth) t.focus();
  else { const box = document.getElementById("box"); if (box) box.focus(); }
}

function openSettings(id) {
  id = typeof id === "string" ? id : "";  // also safe as a bare click handler
  if (!V.h) { pendingOpen = id; return false; }  // before "ready": open once the helpers exist
  if (!id && view && !view.hidden) id = wantedId || currentId;  // already open: stay on this page
  if (!show(id)) return false;
  const want = `#/settings/${wantedId || currentId}`;
  if (location.hash !== want) {
    if (HASH.test(location.hash)) history.replaceState(history.state, "", want);
    else history.pushState({vaaniSettings: true}, "", want);
  }
  return true;
}

function closeSettings() {
  if (HASH.test(location.hash)) {
    // Our own history entry: step back over it so Forward works. Otherwise just drop the hash.
    if (history.state && history.state.vaaniSettings) history.back();
    else history.replaceState(null, "", location.pathname + location.search);
  }
  hide();
}

V.openSettings = openSettings;
V.closeSettings = closeSettings;

window.addEventListener("hashchange", () => {
  if (!V.h) return;  // the ready handler reads the hash
  const m = location.hash.match(HASH);
  if (m) show(m[1] || ""); else hide();
});

// Capture phase, so this sees the rail and drawer before app.js closes them on the same key.
window.addEventListener("keydown", e => {
  if (e.key !== "Escape" || !view || view.hidden || e.defaultPrevented) return;
  if (document.querySelector("dialog[open]")) return;
  const app = document.getElementById("app"), drawer = document.getElementById("drawer");
  if ((app && app.classList.contains("rail-open")) || (drawer && !drawer.hidden)) return;
  closeSettings();
}, true);

V.on("ready", () => {
  const m = location.hash.match(HASH);
  if (m) {
    // A reload keeps history.state; the entry before it is no longer this page, so do not step back to it.
    show(m[1] || "");
    history.replaceState(null, "", currentId ? `#/settings/${wantedId || currentId}` : location.pathname + location.search);
  } else if (pendingOpen !== null) openSettings(pendingOpen);
  pendingOpen = null;
});

V.on("settings:register", p => {
  if (!view || view.hidden) return;
  renderNav();
  if (wantedId && p && p.id === wantedId) show(wantedId);
});

// ---------------------------------------------------------------- built-in pages

function focusLater(root, sel, tries = 30) {
  if (!sel || !root.isConnected) return;
  const n = root.querySelector(sel);
  if (n) n.focus();
  else if (tries) setTimeout(() => focusLater(root, sel, tries - 1), 100);
}

V.settings.register({
  id: "autonomy", title: "Autonomy", icon: "autonomy", order: 10,
  lede: "Vaani earns trust one workflow at a time. Move anything up or down whenever you like.",
  render(c) {
    c.classList.add("sv-autonomy");
    const draw = () => V.panels.autonomy(c);
    draw();
    // An offer's Accept / Not now inside a row calls promote(), which refreshes only the old drawer:
    // redraw here once the request has landed and put focus back on that workflow's level.
    let timer = 0;
    const onClick = e => {
      const b = e.target.closest && e.target.closest(".wf .actions button");
      if (!b) return;
      const sec = b.closest(".wf"), key = sec && sec.getAttribute("aria-labelledby");
      clearTimeout(timer);
      timer = setTimeout(() => {
        if (!c.isConnected) return;
        draw();
        focusLater(c, key ? `[aria-labelledby="${CSS.escape(key)}"] [aria-pressed="true"]` : null);
      }, 700);
    };
    c.addEventListener("click", onClick);
    const off = V.on("autonomy:changed", () => { clearTimeout(timer); if (c.isConnected) draw(); });
    return () => { clearTimeout(timer); off(); };
  },
});

V.settings.register({
  id: "proof", title: "Proof", icon: "proof", order: 20,
  lede: "Every decision Vaani made, newest first. An approved write counts only once Google confirms it.",
  render(c) {
    c.classList.add("sv-proof");
    V.panels.proof(c, "");
  },
});

const SCOPE_NAME = {
  "gmail.modify": "Gmail: read, draft, send and label mail",
  "gmail.readonly": "Gmail: read only",
  "gmail.send": "Gmail: send mail",
  "gmail.compose": "Gmail: drafts and sending",
  "calendar.events": "Calendar: read and change events",
  "calendar.readonly": "Calendar: read only",
  "calendar": "Calendar: full access",
};

const card = (title, ...kids) => h("section", {class: "sv-card"}, title ? h("h2", {class: "sv-card-title", text: title}) : null, ...kids);
const rowsList = rows => h("dl", {class: "sv-rows"}, rows.map(([k, v]) => h("div", {class: "sv-row"}, h("dt", {text: k}), h("dd", {}, v))));
const theme = () => document.documentElement.dataset.theme === "dark" ? "dark" : "light";

function setTheme(value) {
  document.documentElement.dataset.theme = value;
  try { localStorage.setItem("vaani.theme", value); } catch (_) { /* private window: this page only */ }
  V.emit("theme:change", {theme: value});
}

function renderAccount(c) {
  c.classList.add("sv-account");
  const me = V.me;
  if (!me) {
    c.append(card(null, h("p", {class: "sv-note", text: "Not connected to the Vaani server yet. Reload the page once it is running."})));
    return null;
  }
  const u = me.user || {};
  const google = me.auth_mode === "google";
  const email = u.email || me.email || "";
  const name = u.name || (google ? "" : "This Mac");
  const scopes = me.scopes || [];

  // Who
  const av = h("span", {class: "sv-avatar", "aria-hidden": "true", text: (name || email || "?").trim().charAt(0).toUpperCase()});
  if (u.picture) {
    const img = h("img", {src: u.picture, alt: "", referrerpolicy: "no-referrer"});
    img.onerror = () => img.remove();
    av.append(img);
  }
  let signOut = null;
  if (google) {
    signOut = h("button", {type: "button", class: "sv-btn"}, "Sign out");
    signOut.onclick = async () => {
      signOut.disabled = true;
      try { await V.api("/auth/logout", {}); } catch (_) { /* signed out or not, the login page is next */ }
      location.href = "/login";
    };
  }
  c.append(h("section", {class: "sv-card sv-profile", "aria-label": "Signed in as"}, av,
    h("div", {class: "sv-who"}, h("div", {class: "sv-name", text: name || email || "Unknown"}),
      name && email ? h("div", {class: "sv-email", text: email}) : null),
    signOut));

  // Connection
  const model = h("span", {class: "sv-val"}, h("span", {text: me.model || "unknown"}), h("span", {class: "sv-sub", text: " · runs locally"}));
  if (me.model_present === false) model.append(h("span", {class: "sv-tag warn", text: "not loaded"}));
  const clockEl = h("time", {class: "sv-clock"});
  const tick = () => { clockEl.textContent = V.clock(new Date()); };
  tick();
  const timer = setInterval(tick, 1000);
  c.append(card("Connection", rowsList([
    ["Sign-in", google ? "Google account" : "This Mac (sign-in is off, one user)"],
    ["Google permissions", scopes.length ? h("span", {class: "sv-tags"}, scopes.map(s => h("span", {class: "sv-tag", title: s, text: SCOPE_NAME[s] || s})))
      : h("span", {class: "sv-sub", text: "None"})],
    ["Model", model],
    ["Time zone", h("span", {class: "sv-val"}, h("span", {text: me.tz || "unknown"}), h("span", {class: "sv-sub", text: " · now "}), clockEl)],
  ]), me.model_present === false ? h("p", {class: "sv-note warn", role: "status", text: "The local model is not running, so Vaani cannot answer. Start it, then reload this page."}) : null));

  // Appearance
  const seg = h("div", {class: "sv-seg", role: "group", "aria-label": "Theme"});
  const sync = () => { for (const b of seg.children) b.setAttribute("aria-pressed", String(b.dataset.value === theme())); };
  for (const [value, label] of [["light", "Light"], ["dark", "Dark"]]) {
    seg.append(h("button", {type: "button", "data-value": value, onclick: () => { setTheme(value); sync(); }}, label));
  }
  sync();
  const watch = new MutationObserver(sync);  // the rail's theme toggle flips it too
  watch.observe(document.documentElement, {attributes: true, attributeFilter: ["data-theme"]});
  c.append(card("Appearance", h("div", {class: "sv-line"},
    h("div", {}, h("div", {class: "sv-label", text: "Theme"}), h("div", {class: "sv-sub", text: "Saved in this browser."})), seg)));

  // Details: what the old header status line showed, verbatim.
  c.append(card("Details", rowsList([
    ["Connected as", email || "nobody yet"],
    ["Scopes", scopes.join(", ") || "none"],
    ["Model", `${me.model || "unknown"} (local)${me.model_present === false ? " · WARNING: model not loaded" : ""}`],
    ["Now", `${me.now || ""} ${me.tz || ""}`.trim() || "unknown"],
  ])));
  c.lastChild.classList.add("sv-details");

  return () => { clearInterval(timer); watch.disconnect(); };
}

V.settings.register({
  id: "account", title: "Account", icon: "account", order: 30,
  lede: "Who Vaani works for, what Google lets it touch, and how this page looks.",
  render: renderAccount,
});
})();
