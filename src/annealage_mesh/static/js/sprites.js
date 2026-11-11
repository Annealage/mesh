/**
 * Canvas-texture sprite builders shared by pins.js (pin and callout markers)
 * and measure.js (the measurement pill), and the matching teardown.
 *
 * Every builder here allocates a CanvasTexture, which is a GPU texture, and a
 * SpriteMaterial to hold it. Removing the sprite from its parent group drops
 * the scene reference but frees neither, so `disposeSprite` lives next to the
 * builders to keep the pairing visible: a caller that creates a sprite is the
 * caller that has to dispose it. This matters most for agent callouts, whose
 * markers are torn down and rebuilt every time the callout list changes, so a
 * long review session would otherwise accumulate one 64-pixel-square texture
 * plus a material per callout per change.
 */

import * as THREE from "three";

const MONO = "'Red Hat Mono', ui-monospace, monospace";

/**
 * A chamfered plate marker sprite carrying a short label (a pin number, an
 * axis letter): one corner cut at 45 degrees, as the library's pin plates.
 * `fill`, `edge` and `ink` are CSS colour strings; `edge` is optional, `ring`
 * (optional) draws a larger chamfered outline around the plate.
 */
export function makeLabelSprite(text, { fill, edge, ink, ring }) {
  const c = document.createElement("canvas");
  c.width = 64;
  c.height = 64;
  const ctx = c.getContext("2d");
  // Geometry from the library's reference: plate M-10-10H5L10-5V10H-10Z,
  // ring M-14-14H7L14-7V14H-14Z, scaled 2x about the canvas centre.
  const path = (h, cut) => {
    ctx.beginPath();
    ctx.moveTo(32 - h, 32 - h);
    ctx.lineTo(32 + h - cut, 32 - h);
    ctx.lineTo(32 + h, 32 - h + cut);
    ctx.lineTo(32 + h, 32 + h);
    ctx.lineTo(32 - h, 32 + h);
    ctx.closePath();
  };
  ctx.lineJoin = "miter";
  path(20, 10);
  ctx.fillStyle = fill;
  ctx.fill();
  if (edge) {
    ctx.strokeStyle = edge;
    ctx.lineWidth = 3;
    ctx.stroke();
  }
  if (ring) {
    path(28, 14);
    ctx.strokeStyle = ring;
    ctx.lineWidth = 3;
    ctx.stroke();
  }
  ctx.fillStyle = ink;
  ctx.font = "500 28px " + MONO;
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.fillText(String(text), 32, 34);
  const tex = new THREE.CanvasTexture(c);
  tex.anisotropy = 4;
  return new THREE.Sprite(new THREE.SpriteMaterial({ map: tex, depthTest: false, depthWrite: false }));
}

/**
 * A plain text tag, for longer strings like a measurement readout: mono
 * `ink` text on a `background` rectangle with no outline or radius.
 * `userData.aspect` (width/height of the drawn canvas) lets the caller scale
 * the sprite to a non-square size without distorting the text.
 */
export function makeTagSprite(text, ink, background) {
  const font = "500 40px " + MONO;
  const probe = document.createElement("canvas").getContext("2d");
  probe.font = font;
  const w = Math.ceil(probe.measureText(text).width) + 28;
  const c = document.createElement("canvas");
  c.width = w;
  c.height = 64;
  const ctx = c.getContext("2d");
  ctx.fillStyle = background;
  ctx.fillRect(0, 0, w, 64);
  ctx.fillStyle = ink;
  ctx.font = font;
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.fillText(text, w / 2, 34);
  const tex = new THREE.CanvasTexture(c);
  tex.anisotropy = 4;
  const spr = new THREE.Sprite(new THREE.SpriteMaterial({ map: tex, depthTest: false, depthWrite: false }));
  spr.userData.aspect = w / 64;
  return spr;
}

/**
 * Release a sprite's GPU resources. Safe to call with a null or already-torn-
 * down sprite, so a caller reconciling a list does not need its own guard.
 * The caller is still responsible for removing the sprite from its parent
 * group; this frees what removal does not.
 */
export function disposeSprite(spr) {
  if (!spr || !spr.material) return;
  if (spr.material.map) spr.material.map.dispose();
  spr.material.dispose();
}
