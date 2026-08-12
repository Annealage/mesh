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
