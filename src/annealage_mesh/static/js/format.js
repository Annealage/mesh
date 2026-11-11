/**
 * Number text for the review panel, the status bar and the measure readout.
 *
 * A negative value is written with the true minus sign (U+2212), and a value
 * that rounds to zero is written without one, so a column of coordinates
 * lines up and never shows "-0.0".
 */

/** `v` to `digits` decimals, fixed width of fraction, true minus. */
export function mm(v, digits = 1) {
  const f = Math.pow(10, digits);
  const r = Math.round(v * f) / f;
  return (r === 0 ? 0 : r).toFixed(digits).replace("-", "\u2212");
}

/** `v` to at most `digits` decimals, no trailing zeros, true minus. */
export function mmTrim(v, digits = 1) {
  return String(Number(mm(v, digits).replace("\u2212", "-"))).replace("-", "\u2212");
}
