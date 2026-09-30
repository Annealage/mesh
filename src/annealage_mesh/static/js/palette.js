/**
 * The colours the 3D view draws with, read from CSS custom properties so the
 * scene follows the page's theme and its light/dark switch rather than
 * carrying a second copy of the colours in script.
 *
 * WebGL materials and canvas sprites cannot reference a CSS variable, so each
 * drawing module reads the resolved values here and redraws through
 * `onPaletteChange` when the colour scheme flips. The variables come from the
 * brand tokens (lib/style's tokens.css), which write them with light-dark(),
 * so reading the custom property itself gives back that unresolved text.
 * Each one is resolved instead through a hidden probe element's computed
 * `color`, which yields the rgb() the current scheme (and any data-theme
 * override) picks.
 */

// The part colours are a numbered series so parts stay distinguishable; a
// part keeps its slot for the page's lifetime (see models.js) and the slot
// maps onto whatever series the current scheme defines.
const PART_SLOTS = 8;

const listeners = new Set();

/**
 * Resolves once every stylesheet the page links has loaded. A module script
 * does not wait for stylesheets, and a palette read before the tokens arrive
 * resolves every colour to the inherited text colour, so main.js awaits this
 * before building anything that draws.
 */
export const stylesReady = Promise.all(
  [...document.querySelectorAll('link[rel="stylesheet"]')].map((link) =>
    link.sheet
      ? null
      : new Promise((resolve) => {
          link.addEventListener("load", resolve, { once: true });
          link.addEventListener("error", resolve, { once: true });
        }),
  ),
);

let probe = null;

function read(name) {
  if (!probe) {
    probe = document.createElement("span");
    probe.hidden = true;
    document.body.append(probe);
  }
  probe.style.color = "var(" + name + ")";
  return getComputedStyle(probe).color;
}

/** The current palette, as CSS rgb() colour strings. */
export function palette() {
  const parts = [];
  for (let i = 1; i <= PART_SLOTS; i++) parts.push(read("--mv-part-" + i));
  return {
    view: read("--view"),
    grid: read("--mv-grid"),
    grid2: read("--mv-grid-2"),
    pin: read("--pin"),
    onPin: read("--on-pin"),
    pinSelected: read("--pin-selected"),
    callout: read("--callout"),
    onCallout: read("--on-callout"),
    measure: read("--measure"),
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

// scheme.js fires this for the OS scheme changing and for the human's own
// choice; a forced scheme never reaches matchMedia.
document.documentElement.addEventListener("schemechange", () => {
  const pal = palette();
  listeners.forEach((fn) => fn(pal));
});
