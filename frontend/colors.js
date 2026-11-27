/* The base and secondary colour chosen in Settings (prefs color_base and
 * color_secondary), turned into the CSS variables each theme needs.
 *
 * Any colour stays readable. From one chosen colour, derive() makes per theme:
 *   text  for links, active tabs, focus rings, chart lines: at least 4.5:1
 *         against every background it sits on (page, card, inset, tint). In the
 *         light theme the same hue is made darker until it gets there, in the
 *         dark theme lighter (and a little less saturated).
 *   fill  for filled buttons and selected chips: at least 3:1 against the page
 *         and the cards, so the button's edge shows.
 *   on    the text on a fill: white when that reaches 4.5:1, otherwise black
 *         (one of the two always does: max(contrast) >= 4.58 for any colour).
 *   tint  the soft background behind selected items (the colour, see-through).
 * Gain green and loss red are never touched.
 *
 * Pages paint at once from a copy in localStorage ("so.colors": the choice and
 * its CSS), applied by a tiny inline script in <head>. This file is loaded
 * with `defer` and only runs when the choice changes or the server's copy
 * arrives. tests/test_colors.py runs it in node.
 */
(function (root) {
  "use strict";
  const HEX = /^#[0-9a-f]{6}$/i;
  const DEFAULTS = {base: "#2563eb", secondary: "#7c3aed"};
  // The surfaces coloured text and buttons sit on (the same tokens as the pages' CSS).
  const SURFACES = {
    light: {bg: "#f4f6f9", panel: "#ffffff", panel2: "#eff2f6", panel3: "#e1e7ef"},
    dark: {bg: "#0a0d10", panel: "#12171e", panel2: "#1a212b", panel3: "#243040"},
  };
  const TINT = {light: 0.10, dark: 0.15};   // opacity of the soft background
  const PRESETS = [
    {name: "Standard", base: "#2563eb", secondary: "#7c3aed"},
    {name: "Ocean", base: "#0369a1", secondary: "#0d9488"},
    {name: "Graphite", base: "#334155", secondary: "#0ea5e9"},
    {name: "Indigo and gold", base: "#4338ca", secondary: "#ca8a04"},
    {name: "Teal", base: "#0f766e", secondary: "#6366f1"},
    {name: "Copper", base: "#c2410c", secondary: "#0f766e"},
  ];

  const rgb = h => [1, 3, 5].map(i => parseInt(h.slice(i, i + 2), 16));
  const hex = c => "#" + c.map(v => Math.round(Math.max(0, Math.min(255, v))).toString(16).padStart(2, "0")).join("");
  const lin = v => { v /= 255; return v <= 0.04045 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
  function luminance(h) { const [r, g, b] = rgb(h).map(lin); return 0.2126 * r + 0.7152 * g + 0.0722 * b; }
  function contrast(a, b) {
    const x = luminance(a), y = luminance(b);
    return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05);
  }
  const mix = (a, b, t) => { const p = rgb(a), q = rgb(b); return hex(p.map((v, i) => v + (q[i] - v) * t)); };
  // `color` seen through at `alpha` over `under`
  const over = (color, alpha, under) => mix(under, color, alpha);
  const minContrast = (c, grounds) => Math.min(...grounds.map(g => contrast(c, g)));

  function toHsl(h) {
    const [r, g, b] = rgb(h).map(v => v / 255), mx = Math.max(r, g, b), mn = Math.min(r, g, b), l = (mx + mn) / 2;
    if (mx === mn) return [0, 0, l];
    const d = mx - mn, s = l > 0.5 ? d / (2 - mx - mn) : d / (mx + mn);
    const hh = mx === r ? (g - b) / d + (g < b ? 6 : 0) : mx === g ? (b - r) / d + 2 : (r - g) / d + 4;
    return [hh * 60, s, l];
  }
  function fromHsl(h, s, l) {
    const k = n => (n + h / 30) % 12, a = s * Math.min(l, 1 - l);
    return hex([0, 8, 4].map(n => 255 * (l - a * Math.max(-1, Math.min(k(n) - 3, 9 - k(n), 1)))));
  }

  // The same hue, made darker (towards "black") or lighter (towards "white"), by
  // the least amount that reaches `ratio` on every ground. Every RGB channel
  // moves one way with the lightness, so the search always ends: black and
  // white reach any ratio asked here.
  function reach(color, to, ratio, grounds) {
    if (minContrast(color, grounds) >= ratio) return color;
    const dark = to === "#000000";
    let [h, s, l] = toHsl(color);
    if (!dark) s = Math.min(s, 0.9);              // lighter for dark screens: a little less saturated
    let lo = l, hi = dark ? 0 : 1;                // lo fails, hi passes
    for (let i = 0; i < 24; i++) {
      const mid = (lo + hi) / 2;
      if (minContrast(fromHsl(h, s, mid), grounds) >= ratio) hi = mid; else lo = mid;
    }
    let out = fromHsl(h, s, hi);
    for (let i = 0; i < 64 && minContrast(out, grounds) < ratio; i++) out = mix(out, to, 0.02);   // rounding to whole bytes
    return out;
  }

  function onColor(fill) {
    if (contrast(fill, "#ffffff") >= 4.5) return "#ffffff";
    return contrast(fill, "#0a0d10") >= 4.5 ? "#0a0d10" : "#000000";
  }

  // {light: {text, fill, on, tint}, dark: {...}} for one "#rrggbb" colour.
  function derive(color) {
    if (!HEX.test(color || "")) throw new Error("A colour is written as #rrggbb");
    color = color.toLowerCase();
    const out = {};
    for (const theme of ["light", "dark"]) {
      const s = SURFACES[theme], to = theme === "light" ? "#000000" : "#ffffff";
      const plain = [s.bg, s.panel, s.panel2];      // coloured text never sits on panel3
      // text on the tint as well: the tint is made from the text colour, so settle both together
      // (aims a hair above 4.5 and 3, so blending and rounding in the browser never drop below)
      let text = reach(color, to, 4.55, plain);
      for (let i = 0; i < 40; i++) {
        const tinted = plain.map(g => over(text, TINT[theme], g));
        const next = reach(text, to, 4.55, plain.concat(tinted));
        if (next === text) break;
        text = next;
      }
      const fill = reach(color, to, 3.05, [s.bg, s.panel, s.panel2]);
      const [r, g, b] = rgb(text);
      out[theme] = {text, fill, on: onColor(fill), tint: `rgba(${r},${g},${b},${TINT[theme]})`};
    }
    return out;
  }

  // The CSS that overrides the pages' standard colours; "" when nothing is chosen.
  function css(base, secondary) {
    const parts = {dark: [], light: []};
    const add = (prefix, color) => {
      if (!HEX.test(color || "")) return;
      const d = derive(color);
      for (const t of ["dark", "light"]) {
        const v = d[t];
        parts[t].push(`--${prefix}:${v.text};--${prefix}-bg:${v.tint};--${prefix}-fill:${v.fill};--on-${prefix}:${v.on};`);
      }
    };
    add("accent", base);
    add("accent2", secondary);
    if (!parts.dark.length) return "";
    return `:root{${parts.dark.join("")}}:root[data-theme="light"]{${parts.light.join("")}}`;
  }

  const clean = c => (HEX.test(c || "") ? c.toLowerCase() : "");

  // Use a choice on this page now, and keep it for the next first paint.
  // Returns true when what the page shows changed.
  function apply(base, secondary, keep) {
    base = clean(base); secondary = clean(secondary);
    const text = css(base, secondary);
    let el = document.getElementById("so-colors");
    const before = el ? el.textContent : "";
    if (text) {
      if (!el) { el = document.createElement("style"); el.id = "so-colors"; document.head.appendChild(el); }
      el.textContent = text;
    } else if (el) el.remove();
    if (keep !== false) {
      try {
        if (text) localStorage.setItem("so.colors", JSON.stringify({base, secondary, css: text}));
        else localStorage.removeItem("so.colors");
      } catch (e) {}
    }
    return before !== text;
  }

  // What this device last used: {base, secondary} ("" = standard).
  function saved() {
    try { const c = JSON.parse(localStorage.getItem("so.colors") || "null"); if (c) return {base: clean(c.base), secondary: clean(c.secondary)}; }
    catch (e) {}
    return {base: "", secondary: ""};
  }

  root.SOColors = {DEFAULTS, PRESETS, SURFACES, derive, css, apply, saved, contrast, luminance, mix};
})(typeof window !== "undefined" ? window : globalThis);
