/* Live steps for a chat turn (STREAM). Contract: .build/CONTRACT-v2.md.
 *
 * Vaani.streamTurn(body, {spin}) posts the turn to /chat/stream and lists each tool step
 * ("Reading your inbox…") right after the spinner while the turn runs. It resolves with
 * exactly the dict POST /chat returns: the "result" event, the only place the reply text
 * ever arrives (after every server-side guard ran). If the stream fails before a result
 * (older server, network, bad data) it falls back to POST /chat by itself; a 401 goes to
 * /login like Vaani.api. Afterwards the list collapses into one faint line:
 * "Read your inbox · Checked your calendar · 1.4s" (no line when there were no steps).
 * On turn:result the newest reply fades in word by word (≤600 ms; off under reduced motion).
 */
"use strict";
(() => {
  const V = window.Vaani;
  const MAX_WORDS = 400;  // longer replies fade in as one block
  let readyMe = null;     // the /me object handed to "ready"; Vaani.me may not be live
  V.on("ready", d => { readyMe = (d && d.me) || null; });

  const reduced = () => { try { return matchMedia("(prefers-reduced-motion: reduce)").matches; } catch (_) { return false; } };
  const fmtMs = ms => ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${Math.max(0, Math.round(ms || 0))}ms`;

  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  // The live list. Inserted after the spinner on the first step, so a turn with no tool
  // calls leaves nothing behind.
  function stepsList(spin) {
    const box = el("div", "turn-steps");
    const list = box.appendChild(el("ol", "turn-steps-list"));
    const rows = [];
    let placed = false;
    const place = () => {
      if (placed) return;
      placed = true;
      if (spin && spin.parentNode) spin.parentNode.insertBefore(box, spin.nextSibling);
      else (document.getElementById("log") || document.body).appendChild(box);
    };
    function start(ev) {
      place();
      const li = list.appendChild(el("li", "turn-step running"));
      li.appendChild(el("span", "turn-step-mark")).setAttribute("aria-hidden", "true");
      const text = li.appendChild(el("span", "turn-step-label", `${ev.label || "Working"}…`));
      const row = {tool: ev.tool, label: ev.label || "Working", li, text, done: false};
      rows.push(row);
      if (V.scrollDown) V.scrollDown();
      return row;
    }
    function end(ev) {
      let row = null;
      for (let i = rows.length - 1; i >= 0 && !row; i--) if (!rows[i].done && rows[i].tool === ev.tool) row = rows[i];
      row = row || start(ev);
      row.done = true;
      row.ok = ev.ok !== false;
      row.summary = row.ok ? (ev.done || row.label) : `${row.label} (failed)`;
      row.li.classList.remove("running");
      row.li.classList.add(row.ok ? "ok" : "failed");
      row.text.textContent = row.ok ? (ev.done || row.label) : `${row.label}: failed`;
      row.li.appendChild(el("span", "turn-step-ms", fmtMs(ev.ms)));
    }
    function collapse(totalMs) {
      if (!rows.length) { box.remove(); return; }
      const names = [];
      for (const r of rows) {
        const name = r.summary || r.label;
        if (!names.includes(name)) names.push(name);
      }
      box.textContent = "";
      box.className = "turn-steps collapsed";
      box.appendChild(el("span", "turn-steps-summary", names.join(" · ")));
      box.appendChild(el("span", "turn-steps-ms", ` · ${fmtMs(totalMs)}`));
    }
    return {start, end, collapse, remove: () => box.remove()};
  }

  // Calls onEvent with each `data:` JSON of a text/event-stream body (events end with a blank line).
  async function readEvents(res, onEvent) {
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    const flush = block => {
      const data = block.split(/\r?\n/).filter(l => l.startsWith("data:")).map(l => l.slice(5).replace(/^ /, "")).join("\n");
      if (data) onEvent(JSON.parse(data));
    };
    for (;;) {
      const {value, done} = await reader.read();
      buf += done ? dec.decode() : dec.decode(value, {stream: true});
      let m;
      while ((m = /\r?\n\r?\n/.exec(buf))) {
        flush(buf.slice(0, m.index));
        buf = buf.slice(m.index + m[0].length);
      }
      if (done) break;
    }
    if (buf.trim()) flush(buf);
  }

  // One attempt at the stream. Returns {result}, {status: 401}, or null (fall back to POST /chat).
  async function attempt(body, steps, url) {
    const headers = {"Content-Type": "application/json"};
    const me = V.me || readyMe;
    if (me && me.csrf) headers["X-CSRF-Token"] = me.csrf;
    const res = await fetch(url, {method: "POST", headers, credentials: "same-origin", body: JSON.stringify(body)});
    if (res.status === 401) return {status: 401};
    if (!res.ok && url === "/voice/turn") {  // a plain JSON refusal (voice mode off, unknown session): say why
      const j = await res.json().catch(() => ({}));
      const e = new Error(j.error || `/voice/turn failed (${res.status})`);
      e.status = res.status;
      throw e;
    }
    if (!res.ok || !res.body || !/text\/event-stream/.test(res.headers.get("content-type") || "")) return null;
    let out = null;
    try {
      await readEvents(res, ev => {
        if (!ev || typeof ev !== "object") return;
        if (ev.type === "step") {
          V.emit("turn:step", ev);
          if (ev.phase === "end") steps.end(ev); else steps.start(ev);
        } else if (ev.type === "result") {
          out = {result: ev.result};
        } else if (ev.type === "error" && !out) {
          throw new Error(ev.error || "the stream failed");
        }
      });
    } catch (e) {
      if (!out) throw e;  // the turn already answered: never run it a second time
    }
    return out;
  }

  // opts.path: "/chat" (default; streams through /chat/stream, falls back to POST /chat) or
  // "/voice/turn" (voice mode, contract v3: already a stream; no fallback, a failure throws).
  V.streamTurn = async (body, opts = {}) => {
    const t0 = performance.now();
    const steps = stepsList(opts.spin);
    const voice = opts.path === "/voice/turn";
    let out = null;
    try {
      out = await attempt(body, steps, voice ? "/voice/turn" : "/chat/stream");
    } catch (e) {
      if (voice) { steps.remove(); throw e; }
      console.warn("Vaani: /chat/stream failed, using /chat", e);
      out = null;
    }
    if (out && out.status === 401) {
      steps.remove();
      location.href = "/login";
      const e = new Error("sign in required");
      e.status = 401;
      throw e;
    }
    if (!out || !out.result || typeof out.result !== "object") {
      steps.remove();
      if (voice) throw new Error("the voice turn did not answer");
      return V.api("/chat", body);
    }
    steps.collapse(performance.now() - t0);
    return out.result;
  };

  // ---- reply reveal: the words of the newest reply appear one after another, then the
  // wrapper spans are swapped back for the original text nodes.
  function reveal(target) {
    const walker = document.createTreeWalker(target, NodeFilter.SHOW_TEXT);
    const texts = [];
    for (let n = walker.nextNode(); n; n = walker.nextNode()) if (n.nodeValue.trim()) texts.push(n);
    let count = 0;
    for (const t of texts) count += t.nodeValue.split(/\s+/).filter(Boolean).length;
    if (!count) return;
    if (count > MAX_WORDS) {
      target.classList.add("reveal-fade");
      setTimeout(() => target.classList.remove("reveal-fade"), 650);
      return;
    }
    const step = Math.min(30, 450 / count);  // last word starts by 450 ms and takes 150 ms
    const swaps = [];
    let i = 0;
    for (const t of texts) {
      const frag = document.createDocumentFragment();
      const made = [];
      for (const part of t.nodeValue.split(/(\s+)/)) {
        if (!part) continue;
        let node;
        if (/^\s+$/.test(part)) node = document.createTextNode(part);
        else { node = el("span", "sw", part); node.style.animationDelay = `${Math.round(i++ * step)}ms`; }
        made.push(node);
        frag.appendChild(node);
      }
      t.parentNode.replaceChild(frag, t);
      swaps.push({t, made});
    }
    target.classList.add("revealing");
    setTimeout(() => {
      for (const {t, made} of swaps) {
        const first = made[0];
        if (!first || !first.parentNode) continue;
        first.parentNode.insertBefore(t, first);
        for (const m of made) m.remove();
      }
      target.classList.remove("revealing");
    }, 650);
  }

  V.on("turn:result", data => {
    const r = data && data.r;
    if (!r || !r.reply || reduced()) return;
    const log = document.getElementById("log");
    const bots = log ? log.querySelectorAll(".msg.bot:not(.err)") : [];
    const target = bots[bots.length - 1];
    if (target) { try { reveal(target); } catch (e) { console.warn("Vaani: reply reveal failed", e); } }
  });
})();
