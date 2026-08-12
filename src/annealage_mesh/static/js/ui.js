/**
 * Toast and the error banner: two small pieces of DOM feedback used by
 * pins.js (submit result, copy-to-clipboard result) and models.js (fetch
 * and load failures).
 */

/** Transient bottom-of-screen toast, auto-dismissed after 3.5s. */
export function toast(msg, ok) {
  const t = document.getElementById("toast");
  t.textContent = msg;
  t.className = ok ? "ok" : "err";
  t.style.display = "block";
  clearTimeout(toast._t);
  toast._t = setTimeout(() => {
    t.style.display = "none";
  }, 3500);
}

/** Persistent bottom-left error banner, for failures the user should keep seeing. */
export function showError(msg) {
  const e = document.getElementById("err");
  e.textContent = msg;
  e.style.display = "block";
}


/**
 * Build the two-line "part - label / coordinates" block used by the pin list
 * and the callout list.
 *
 * Every value here is untrusted. A pin's `part` comes from an STL filename, and
 * a callout's `part` and `label` come from mesh-callouts.json, which the agent
 * writes and /callouts serves verbatim with no field validation. Interpolating
 * either into innerHTML puts script execution in the viewer's own origin, and
 * that origin can drive the chat composer, so a crafted filename or callout
 * turns into an arbitrary instruction to an agent holding a shell. Text nodes
 * cannot do that, which is why this function exists rather than a template
 * string at each call site.
 */
export function buildMetaRows(partText, labelText, point, round) {
  const meta = document.createElement("div");
  meta.className = "meta";

  const part = document.createElement("div");
  part.className = "part";
  part.textContent = partText;
  if (labelText) {
    part.append(document.createTextNode(" \u00b7 "));
    const label = document.createElement("span");
    label.className = "label";
    label.textContent = labelText;
    part.append(label);
  }

  const loc = document.createElement("div");
  loc.className = "loc";
  loc.textContent = "[" + point.map((v) => round(v, 2)).join(", ") + "] mm";

  meta.append(part, loc);
  return meta;
}
