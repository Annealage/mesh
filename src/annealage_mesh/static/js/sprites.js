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

/** A round badge sprite carrying a short label (a pin number, an axis letter). */
export function makeLabelSprite(text, hexColor) {
  const c = document.createElement("canvas");
  c.width = 64;
  c.height = 64;
  const ctx = c.getContext("2d");
  ctx.fillStyle = hexColor;
  ctx.beginPath();
  ctx.arc(32, 32, 30, 0, Math.PI * 2);
  ctx.fill();
  ctx.fillStyle = "#14161a";
  ctx.font = "bold 34px system-ui, sans-serif";
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.fillText(String(text), 32, 34);
  const tex = new THREE.CanvasTexture(c);
  tex.anisotropy = 4;
  return new THREE.Sprite(new THREE.SpriteMaterial({ map: tex, depthTest: false, depthWrite: false }));
}

/**
 * A pill-shaped text tag, for longer strings like a measurement readout,
 * distinct from makeLabelSprite's round number badge. `userData.aspect`
 * (width/height of the drawn canvas) lets the caller scale the sprite to a
 * non-square size without distorting the text.
 */
export function makeTagSprite(text, hexColor) {
  const font = "bold 40px system-ui, sans-serif";
  const probe = document.createElement("canvas").getContext("2d");
  probe.font = font;
  const w = Math.ceil(probe.measureText(text).width) + 28;
  const c = document.createElement("canvas");
  c.width = w;
  c.height = 64;
  const ctx = c.getContext("2d");
  const pill = (inset) => {
    const r = 14 - inset, x = inset, y = inset, ww = w - inset * 2, hh = 64 - inset * 2;
    ctx.beginPath();
    ctx.moveTo(x + r, y);
    ctx.arcTo(x + ww, y, x + ww, y + hh, r);
    ctx.arcTo(x + ww, y + hh, x, y + hh, r);
    ctx.arcTo(x, y + hh, x, y, r);
    ctx.arcTo(x, y, x + ww, y, r);
    ctx.closePath();
  };
  ctx.fillStyle = "#14161ae0";
  pill(0);
  ctx.fill();
  ctx.strokeStyle = hexColor;
  ctx.lineWidth = 3;
  pill(1.5);
  ctx.stroke();
  ctx.fillStyle = hexColor;
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
