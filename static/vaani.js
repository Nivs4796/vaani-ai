/* Vaani hook bus: the one shared object every front-end file talks through.
 *
 * Loaded first. Feature files (brief.js, settings.js, stream.js, history.js, rail.js,
 * voice.js) attach their entry points here; app.js loads last, adds the shared helpers
 * and emits "ready" once /me has loaded. Do startup work in Vaani.on("ready", fn), never
 * at load time: the helpers do not exist yet when a feature file runs.
 * Contract: .build/CONTRACT-v2.md.
 */
"use strict";
(() => {
  const handlers = {};
  let readyData = null;
  const sortByOrder = list => list.sort((a, b) => (a.order || 0) - (b.order || 0));
  const V = window.Vaani = {
    on(evt, fn) {
      (handlers[evt] = handlers[evt] || []).push(fn);
      if (evt === "ready" && readyData) { try { fn(readyData); } catch (e) { console.error("Vaani ready handler failed", e); } }
      return () => { handlers[evt] = (handlers[evt] || []).filter(f => f !== fn); };
    },
    emit(evt, data) {
      if (evt === "ready") readyData = data || {};
      for (const fn of (handlers[evt] || []).slice()) {
        try { fn(data); } catch (e) { console.error(`Vaani ${evt} handler failed`, e); }
      }
    },
    // Side-menu sections: {id, title, icon, order, load: async () => ({count, items: [{title, sub, onClick}], empty})}
    rail: { sections: [], register(s) { V.rail.sections.push(s); sortByOrder(V.rail.sections); V.emit("rail:register", s); } },
    // Settings pages: {id, title, icon, order, render: (container) => void}
    settings: { pages: [], register(p) { V.settings.pages.push(p); sortByOrder(V.settings.pages); V.emit("settings:register", p); } },
  };
})();
