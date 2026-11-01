/* Shared helpers of our AstrBot plugin views (a copy lives in each plugin; the source is docs/panel-kit/).
   Everything that came from chats or the web is put in with textContent, never as HTML. */
(function () {
  "use strict";

  const bridge = window.AstrBotPluginView || window.AstrBotPluginPage;
  const SVG = "http://www.w3.org/2000/svg";

  function el(tag, attrs, ...children) {
    const node = document.createElement(tag);
    setAttrs(node, attrs);
    for (const c of children.flat(Infinity)) {
      if (c === null || c === undefined || c === false) continue;
      node.append(c instanceof Node ? c : document.createTextNode(String(c)));
    }
    return node;
  }

  function svg(tag, attrs, ...children) {
    const node = document.createElementNS(SVG, tag);
    setAttrs(node, attrs);
    for (const c of children.flat(Infinity)) {
      if (c === null || c === undefined || c === false) continue;
      node.append(c instanceof Node ? c : document.createTextNode(String(c)));
    }
    return node;
  }

  function setAttrs(node, attrs) {
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === undefined || v === null || v === false) continue;
      if (k === "class") node.setAttribute("class", v);
      else if (k === "on") for (const [ev, fn] of Object.entries(v)) node.addEventListener(ev, fn);
      else node.setAttribute(k, v === true ? "" : v);
    }
  }

  function clear(node, ...children) {
    node.replaceChildren(...children.flat(Infinity).filter((c) => c !== null && c !== undefined && c !== false));
    return node;
  }

  let errorBox = null;
  function showError(message) {
    errorBox = errorBox || document.getElementById("error");
    if (!errorBox) return;
    if (!message) { errorBox.hidden = true; return; }
    errorBox.textContent = message;
    errorBox.hidden = false;
  }

  function makeApi(pluginLabel) {
    return async function api(method, endpoint, payload) {
      if (!bridge) throw new Error(`请在 AstrBot 后台的「插件 → ${pluginLabel} → 视图」里打开这个页面`);
      try {
        const data = method === "GET"
          ? await bridge.apiGet("page/" + endpoint, payload || {})
          : await bridge.apiPost("page/" + endpoint, payload || {});
        showError("");
        return data;
      } catch (err) {
        showError("出错了：" + (err && err.message ? err.message : err));
        throw err;
      }
    };
  }

  const pad = (n) => String(n).padStart(2, "0");
  function time(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    if (isNaN(d)) return String(iso);
    const year = d.getFullYear() !== new Date().getFullYear() ? d.getFullYear() + "-" : "";
    return `${year}${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
  }
  function clock(iso) {
    const d = new Date(iso);
    return isNaN(d) ? String(iso || "") : `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  }
  function day(iso) {
    const d = new Date(iso.length === 10 ? iso + "T00:00:00" : iso);
    return isNaN(d) ? String(iso || "") : `${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  }
  function ago(iso) {
    const d = new Date(iso);
    if (isNaN(d)) return "";
    const m = Math.round((Date.now() - d.getTime()) / 60000);
    if (m < 1) return "刚刚";
    if (m < 60) return `${m} 分钟前`;
    if (m < 60 * 24) return `${Math.round(m / 60)} 小时前`;
    return `${Math.round(m / 1440)} 天前`;
  }

  function card(label, value, sub, cls) {
    return el("div", { class: "card" + (cls ? " " + cls : "") },
      el("div", { class: "n" }, value), el("div", { class: "l" }, label), sub ? el("div", { class: "s" }, sub) : null);
  }

  function badge(text, cls) {
    return el("span", { class: "badge" + (cls ? " " + cls : "") }, text);
  }

  function empty(text) {
    return el("div", { class: "empty" }, text);
  }

  /* A round step so that at most `count` steps cover v: returns {top, ticks: [0, step, ..., top]}. */
  function niceScale(v, count) {
    count = count || 5;
    if (!(v > 0)) return { top: 1, ticks: [0, 1] };
    const raw = v / count, p = Math.pow(10, Math.floor(Math.log10(raw)));
    let step = 10 * p;
    for (const m of [1, 2, 2.5, 5, 10]) if (m * p >= raw) { step = m * p; break; }
    const n = Math.max(1, Math.ceil(v / step - 1e-9));
    return { top: n * step, ticks: Array.from({ length: n + 1 }, (_, i) => i * step) };
  }

  /* points: [{t: ISO time, v: number, mark?: string}] -> a line over time. opts: {height, fmt, marks} */
  function lineChart(points, opts) {
    opts = opts || {};
    const W = opts.width || 640, H = opts.height || 180, L = 44, R = 10, T = 10, B = 24;
    const fmt = opts.fmt || ((v) => String(v));
    if (!points.length) return empty(opts.emptyText || "还没有数据");
    const ts = points.map((p) => new Date(p.t).getTime());
    const t0 = Math.min(...ts), t1 = Math.max(...ts, t0 + 1);
    const { top, ticks } = niceScale(Math.max(...points.map((p) => p.v)) * 1.05);
    const x = (t) => L + ((t - t0) / (t1 - t0)) * (W - L - R);
    const y = (v) => T + (1 - v / top) * (H - T - B);
    const grid = svg("g", { class: "grid" });
    for (const v of ticks) {
      grid.append(svg("line", { x1: L, x2: W - R, y1: y(v), y2: y(v) }),
        svg("text", { x: L - 6, y: y(v) + 4, "text-anchor": "end" }, fmt(v)));
    }
    const xs = [t0, (t0 + t1) / 2, t1];
    xs.forEach((t, i) => grid.append(svg("text", {
      x: x(t), y: H - 6, "text-anchor": i === 0 ? "start" : i === 2 ? "end" : "middle",
    }, time(new Date(t).toISOString()))));
    const path = points.map((p, i) => `${i ? "L" : "M"}${x(ts[i]).toFixed(1)},${y(p.v).toFixed(1)}`).join(" ");
    const area = `${path} L${x(ts[ts.length - 1]).toFixed(1)},${y(0)} L${x(ts[0]).toFixed(1)},${y(0)} Z`;
    const dots = points.map((p, i) => svg("circle", { class: p.mark ? "mark" : "dot", cx: x(ts[i]), cy: y(p.v), r: p.mark ? 4 : 2.5 },
      svg("title", {}, `${time(p.t)}  ${fmt(p.v)}${p.mark ? "  " + p.mark : ""}`)));
    return svg("svg", { class: "chart", viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": opts.label || "" },
      grid, svg("path", { class: "area", d: area }), svg("path", { class: "line", d: path }), dots);
  }

  /* rows: [{label, parts: [{v, cls, name}]}] -> stacked bars. opts: {height, fmt, note(row) -> text above the bar} */
  function barChart(rows, opts) {
    opts = opts || {};
    const W = opts.width || 640, H = opts.height || 180, L = 44, R = 10, T = 16, B = 24;
    const fmt = opts.fmt || ((v) => String(v));
    if (!rows.length) return empty(opts.emptyText || "还没有数据");
    const total = (r) => r.parts.reduce((s, p) => s + (p.v || 0), 0);
    const { top, ticks } = niceScale(Math.max(...rows.map(total)) * 1.05);
    const y = (v) => T + (1 - v / top) * (H - T - B);
    const slot = (W - L - R) / rows.length, bw = Math.min(42, slot * 0.6);
    const grid = svg("g", { class: "grid" });
    for (const v of ticks) {
      grid.append(svg("line", { x1: L, x2: W - R, y1: y(v), y2: y(v) }),
        svg("text", { x: L - 6, y: y(v) + 4, "text-anchor": "end" }, fmt(v)));
    }
    const bars = svg("g", {});
    rows.forEach((r, i) => {
      const cx = L + slot * i + slot / 2;
      let acc = 0;
      for (const p of r.parts) {
        const v = p.v || 0;
        if (v <= 0) continue;
        bars.append(svg("rect", { class: p.cls || "s1", x: cx - bw / 2, width: bw, y: y(acc + v), height: y(acc) - y(acc + v), rx: 2 },
          svg("title", {}, `${r.label}  ${p.name ? p.name + " " : ""}${fmt(v)}`)));
        acc += v;
      }
      if (opts.note) {
        const n = opts.note(r);
        if (n) bars.append(svg("text", { x: cx, y: y(acc) - 4, "text-anchor": "middle" }, n));
      }
      if (!opts.every || i % opts.every === 0 || i === rows.length - 1) {
        bars.append(svg("text", { x: cx, y: H - 6, "text-anchor": "middle" }, r.label));
      }
    });
    return svg("svg", { class: "chart", viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": opts.label || "" }, grid, bars);
  }

  function legend(items) {
    return el("div", { class: "legend" }, items.map(([cls, name]) =>
      el("span", {}, el("i", { style: `background: var(--${cls})` }), name)));
  }

  function busy(button, fn) {
    return async (...args) => {
      button.disabled = true;
      try { return await fn(...args); } finally { button.disabled = false; }
    };
  }

  function applyTheme(ctx) {
    if (ctx && typeof ctx.isDark === "boolean") {
      document.documentElement.setAttribute("data-theme", ctx.isDark ? "dark" : "light");
    }
  }

  /* Start the page once AstrBot's bridge is ready (and follow the dashboard's light / dark theme). */
  function onReady(pluginLabel, fn) {
    const start = () => {
      if (!bridge) {
        showError(`请在 AstrBot 后台的「插件 → ${pluginLabel} → 视图」里打开这个页面`);
        return;
      }
      bridge.onContext(applyTheme);
      bridge.ready().then(applyTheme).catch(() => {}).finally(fn);
    };
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
    else start();
  }

  window.PanelKit = { bridge, el, svg, clear, showError, makeApi, time, clock, day, ago, card, badge, empty,
    lineChart, barChart, legend, busy, onReady };
})();
