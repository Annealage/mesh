/**
 * The colours the 3D view draws with, read from CSS custom properties so the
 * scene follows the page's theme and its light/dark switch rather than
 * carrying a second copy of the colours in script.
 *
 * WebGL materials and canvas sprites cannot reference a CSS variable, so each
 * drawing module reads the resolved values here and redraws through
 * `onPaletteChange` when the colour scheme flips. The variables are defined
 * by the stylesheet (app.css, overridable by the brand tokens); a missing one
 * resolves to an empty string, which `THREE.Color` and canvas both reject
 * loudly rather than silently drawing black.
 */

// The part colours are a numbered series so parts stay distinguishable; a
// part keeps its slot for the page's lifetime (see models.js) and the slot
// maps onto whatever series the current scheme defines.
const PART_SLOTS = 8;

const listeners = new Set();

function read(style, name) {
  return style.getPropertyValue(name).trim();
}

/** The current palette, as CSS colour strings. */
export function palette() {
  const s = getComputedStyle(document.documentElement);
  const parts = [];
  for (let i = 1; i <= PART_SLOTS; i++) parts.push(read(s, "--mv-part-" + i));
  return {
    view: read(s, "--view"),
    grid: read(s, "--mv-grid"),
    grid2: read(s, "--mv-grid-2"),
    pin: read(s, "--pin"),
    onPin: read(s, "--on-pin"),
    pinSelected: read(s, "--pin-selected"),
    callout: read(s, "--callout"),
    onCallout: read(s, "--on-callout"),
    measure: read(s, "--measure"),
    parts,
  };
}

/** The colour for part slot `slot`, cycling through the series. */
export function partColor(pal, slot) {
  return pal.parts[slot % pal.parts.length];
}

/** Call `fn(palette())` whenever the colour scheme changes. */
export function onPaletteChange(fn) {
  listeners.add(fn);
}

matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
  const pal = palette();
  listeners.forEach((fn) => fn(pal));
});
