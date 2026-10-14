/* orb.js: the HUD's core, the ringed orb from the approved mock-up. Contract: .build/CONTRACT-v3.md (CORE).
 *
 * Vaani.orb = {mount(el), setPhase(phase, {count}), setLevel(mic, out)}. The orb is one real
 * <button class="orb-btn"> holding the ring SVG; a click calls Vaani.hands.tapCore(). It follows
 * voice:phase {phase, count} and voice:level {mic, out} on the hook bus by itself, so the HUD only
 * has to mount it into #orbMount (on "ready" it finds #orbMount itself when the HUD has not).
 * Phases (data-phase on the button; orb.css draws them): asleep standby listening working speaking
 * approval countdown done. The countdown ring drains one step for each `count` ENGINE sends
 * (5..1), each over 1 s, so a held countdown (no new count) freezes the ring. The voice level
 * becomes --orb-scale / --orb-glow, at most once per animation frame; nothing moves under
 * prefers-reduced-motion.
 */
"use strict";
(() => {
  const V = window.Vaani;
  const SVG = "http://www.w3.org/2000/svg";
  const PHASES = ["asleep", "standby", "listening", "working", "speaking", "approval", "countdown", "done"];
  const LABELS = {asleep: "Wake Vaani", speaking: "Interrupt Vaani", countdown: "Stop the countdown"};
  const RINGS = [["r-outer", 152], ["r-ticks", 136], ["r-mid", 116], ["r-sweep", 116], ["r-count", 98],
    ["r-inner", 80], ["ripple rp1", 58], ["ripple rp2", 58]];
  const CIRC = 2 * Math.PI * 98;  // the countdown ring (r 98), ~616 like the mock-up
  const COUNT_S = 5;              // voicestate.COUNTDOWN_S: a countdown that starts at 5 starts on a full ring
  const LEVEL_IDLE_MS = 300;      // no voice:level for this long -> the core settles back
  const OBSERVE_MS = 20000;       // how long "ready" waits for the HUD to create #orbMount

  let btn = null, ring = null, vText = null, numText = null;
  let phase = "asleep", heardPhase = false;
  let count = null, total = COUNT_S, frozen = false;
  let mic = 0, out = 0, raf = 0, idleTimer = 0, shown = -1;
  let observer = null, observeTimer = 0;

  const motionQuery = (() => { try { return matchMedia("(prefers-reduced-motion: reduce)"); } catch (_) { return null; } })();
  const reduced = () => !!(motionQuery && motionQuery.matches);
  const clamp = x => { x = Number(x); return isFinite(x) ? Math.max(0, Math.min(1, x)) : 0; };
  const frame = fn => (window.requestAnimationFrame ? requestAnimationFrame(fn) : setTimeout(fn, 16));

  // ---------------------------------------------------------------- DOM (createElementNS, never innerHTML)
  function circle(cls, r) {
    const c = document.createElementNS(SVG, "circle");
    c.setAttribute("class", cls);
    c.setAttribute("cx", "160");
    c.setAttribute("cy", "160");
    c.setAttribute("r", String(r));
    return c;
  }

  function span(cls, text) {
    const s = document.createElement("span");
    s.className = cls;
    s.textContent = text;
    return s;
  }

  function build() {
    btn = document.createElement("button");
    btn.type = "button";
    btn.className = "orb-btn";
    const svg = document.createElementNS(SVG, "svg");
    svg.setAttribute("class", "orb-core");
    svg.setAttribute("viewBox", "0 0 320 320");
    svg.setAttribute("aria-hidden", "true");
    svg.setAttribute("focusable", "false");
    for (const [cls, r] of RINGS) svg.append(circle(cls, r));
    // The level group scales the core with the voice; r-glow (invisible at rest) brightens it.
    const lvl = document.createElementNS(SVG, "g");
    lvl.setAttribute("class", "orb-lvl");
    lvl.append(circle("r-glow", 50), circle("r-core", 50));
    svg.append(lvl);
    ring = svg.querySelector(".r-count");
    ring.style.strokeDasharray = `${CIRC.toFixed(2)} 1000`;
    const mid = span("orb-mid", "");
    mid.setAttribute("aria-hidden", "true");
    vText = mid.appendChild(span("orb-v", "V"));
    numText = mid.appendChild(span("orb-num", ""));
    btn.append(svg, mid);
    btn.addEventListener("click", () => {
      try { if (V.hands && typeof V.hands.tapCore === "function") V.hands.tapCore(); }
      catch (e) { console.error("Vaani orb tap failed", e); }
    });
    if (!heardPhase) {
      try { const p = V.hands && V.hands.phase; if (PHASES.includes(p)) phase = p; } catch (_) { /* keep asleep */ }
    }
    jump(phase === "countdown" && count != null ? stepEnd() : 1);
    shown = -1;
    applyLevel();
  }

  function paint(reattach) {
    if (!btn) return;
    if (reattach && !btn.isConnected) {  // the HUD re-rendered its stage: go back into the new #orbMount
      const el = document.getElementById("orbMount");
      if (el) el.appendChild(btn);
    }
    btn.dataset.phase = phase;
    btn.setAttribute("aria-label", LABELS[phase] || "Start listening");
    const counting = phase === "countdown";
    vText.hidden = counting;
    numText.hidden = !counting;
    numText.textContent = counting && count != null ? String(count) : "";
  }

  function mount(el) {
    if (!el || typeof el.appendChild !== "function") return btn;
    if (!btn) build();
    if (btn.parentNode !== el) el.appendChild(btn);
    stopObserving();
    paint();
    return btn;
  }

  // ---------------------------------------------------------------- countdown ring
  function setRing(frac, animate) {
    if (!ring) return;
    ring.style.transition = animate && !reduced() ? "stroke-dashoffset 1s linear" : "none";
    // (CIRC + 2) so an empty ring leaves no round-cap dot at 12 o'clock.
    ring.style.strokeDashoffset = ((CIRC + 2) * (1 - clamp(frac))).toFixed(2);
  }

  function jump(frac) {
    setRing(frac, false);
    if (ring) void getComputedStyle(ring).strokeDashoffset;  // commit the jump before any transition starts
  }

  // Where the ring stands at the end of the current second (reduced motion: the still fraction left).
  const stepEnd = () => (reduced() ? count : count - 1) / total;

  function freeze() {
    frozen = true;
    if (!ring) return;
    const off = parseFloat(getComputedStyle(ring).strokeDashoffset);
    if (!isFinite(off)) return;
    ring.style.transition = "none";
    ring.style.strokeDashoffset = off.toFixed(2);
  }

  // One countdown step. A first count, or one larger than the last (a new countdown), starts from
  // its own fraction of a full ring; each lower count drains to the end of that second over 1 s.
  // The same count again changes nothing, so the ring rests where ENGINE stopped sending counts.
  // An optional {held: true} (speech onset holds the countdown) freezes it on the spot.
  function countStep(fresh, c, held) {
    if (c == null) c = fresh || count == null ? COUNT_S : count;
    if (fresh || count == null || c > count) {
      total = Math.max(COUNT_S, c);
      count = c;
      frozen = false;
      jump(c / total);
      if (held) freeze(); else setRing(stepEnd(), true);
      return;
    }
    if (held) { count = c; freeze(); return; }
    if (c === count && !frozen) return;
    count = c;
    frozen = false;
    setRing(stepEnd(), true);
  }

  function endCount() {
    count = null;
    frozen = false;
    jump(1);  // hidden outside the countdown; full again for the next one
  }

  function setPhase(p, opts) {
    if (p === "thinking") p = "working";  // the mock-up's name for it
    if (!PHASES.includes(p)) return;
    heardPhase = true;
    const o = opts || {};
    const n = o.count == null || o.count === "" ? null : Number(o.count);
    const c = n == null || !isFinite(n) ? null : Math.max(0, Math.round(n));
    const was = phase;
    phase = p;
    if (p === "countdown") countStep(was !== "countdown", c, !!o.held);
    else if (was === "countdown") endCount();
    if (levelSource() == null) dropLevel();
    paint(true);
  }

  // ---------------------------------------------------------------- voice level
  // Mic while listening, Vaani's voice while speaking; approval (a read-back, then waiting for
  // the word) follows whichever is louder. Other phases keep the core still.
  function levelSource() {
    if (phase === "listening") return "mic";
    if (phase === "speaking") return "out";
    if (phase === "approval") return "both";
    return null;
  }

  function applyLevel() {
    raf = 0;
    if (!btn) return;
    const src = reduced() ? null : levelSource();
    let l = src === "mic" ? mic : src === "out" ? out : src === "both" ? Math.max(mic, out) : 0;
    l = Math.round(l * 50) / 50;
    if (l === shown) return;
    shown = l;
    btn.style.setProperty("--orb-scale", (1 + l * 0.08).toFixed(3));
    btn.style.setProperty("--orb-glow", (l * 0.22).toFixed(3));
  }

  function schedule() { if (!raf) raf = frame(applyLevel); }

  function dropLevel() {
    mic = 0;
    out = 0;
    schedule();
  }

  function setLevel(m, o) {
    mic = clamp(m);
    out = clamp(o);
    clearTimeout(idleTimer);
    idleTimer = setTimeout(dropLevel, LEVEL_IDLE_MS);
    schedule();
  }

  // ---------------------------------------------------------------- self-mount
  function stopObserving() {
    if (observer) { observer.disconnect(); observer = null; }
    clearTimeout(observeTimer);
    observeTimer = 0;
  }

  function tryMount() {
    if (btn && btn.isConnected) return true;
    const el = document.getElementById("orbMount");
    if (!el) return false;
    mount(el);
    return true;
  }

  function autoMount() {
    if (tryMount() || observer) return;
    if (!window.MutationObserver) {  // very old browsers: a few short retries
      let tries = 0;
      const retry = () => { if (!tryMount() && ++tries < 40) observeTimer = setTimeout(retry, 250); };
      observeTimer = setTimeout(retry, 250);
      return;
    }
    observer = new MutationObserver(() => { if (tryMount()) stopObserving(); });
    observer.observe(document.body || document.documentElement, {childList: true, subtree: true});
    observeTimer = setTimeout(stopObserving, OBSERVE_MS);
  }

  if (motionQuery) {
    const onMotion = () => {
      if (phase === "countdown" && count != null && !frozen) jump(stepEnd());
      shown = -1;
      schedule();
    };
    try { motionQuery.addEventListener("change", onMotion); }
    catch (_) { try { motionQuery.addListener(onMotion); } catch (__) { /* no live updates */ } }
  }

  // Bus listeners need no helpers, so they are attached now and catch a phase sent before "ready".
  V.on("voice:phase", d => { if (d) setPhase(d.phase, d); });
  V.on("voice:level", d => { if (d) setLevel(d.mic, d.out); });
  V.on("mode:change", () => { if (btn && !btn.isConnected) tryMount(); });
  V.on("ready", autoMount);

  V.orb = {
    mount, setPhase, setLevel,
    get phase() { return phase; },
    get el() { return btn; },
  };
})();
