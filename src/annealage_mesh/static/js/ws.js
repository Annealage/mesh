/**
 * Browser side of the /ws transport: reads the per-run token out of the URL
 * once, connects, exchanges hello and replay, dispatches server events, and
 * drives the topbar connection indicator. Reconnection uses capped
 * exponential backoff and never stops on its own except for the two cases
 * that retrying cannot fix within this page load: a protocol-version
 * mismatch, and a confirmed token refusal.
 *
 * This module is the only place the token is held; it lives in the
 * TOKEN module variable, never in localStorage or sessionStorage, because a
 * token that outlives the run it belongs to is a liability and a fresh one
 * is generated every server start anyway.
 *
 * store.setCallouts already has one caller, pins.js's refetchCallouts. This
 * module never calls it directly and never writes state itself; it only
 * decides, via startCalloutsPoll/stopCalloutsPoll/refetchCallouts handed in
 * by main.js, whether the poll or the push is the one presently allowed to
 * call it, so the two are never both running at once.
 *
 * `onHello` and `onAgentEvent` are the chat pane's two inbound hooks:
 * `onHello` receives the hello frame's `session` object once per connection
 * (including every reconnect, since agent status can change between them),
 * and `onAgentEvent` receives every event whose kind is not
 * `callouts_changed`. Neither is called from here except at those two
 * points; chat.js, not this module, decides what an event means. The
 * returned `send` is this module's only outbound capability, so a turn,
 * permission or interrupt frame still goes out over the one socket this
 * closure owns, with no second connection or second reconnect policy.
 */

import { store } from "./store.js";
import { showError, toast } from "./ui.js";

const PROTOCOL_VERSION = 1;

const BASE_BACKOFF_MS = 500; // also the "one backoff interval" the fallback poll waits out
const MAX_BACKOFF_MS = 15000;

// How long a connection attempt is given to prove it is alive, from the
// moment connect() constructs the socket: past this, with no message
// (open's own hello included) having reset the clock, the socket is
// treated as dead whether it never reached OPEN at all or opened and then
// went quiet with no close event of its own (a laptop that slept, a NAT
// mapping dropped silently).
//
// This must stay at least twice the server's PING_INTERVAL (http/ws.py,
// currently 5 seconds), because those pings are the only traffic an
// otherwise idle connection carries and they are what distinguishes idle
// from dead. Set below that and a healthy connection with nothing to say
// gets closed and reopened forever; set far above it and a phone that lost
// its network waits too long before falling back to polling. Three missed
// pings is the compromise.
const LIVENESS_TIMEOUT_MS = 15000;

const REFUSED_MESSAGE =
  "This page's link has gone stale (the server was restarted, or this " +
  "tab was opened without the printed link, since a plain reload does not " +
  "keep the token). Reopen the URL printed in the terminal to get a " +
  "working one.";
const MISMATCH_MESSAGE =
  "This page is running an older build than the server now speaks. " +
  "Reload the page to pick up the current one.";

const CONN_LABEL = {
  connecting: "Connecting…",
  live: "Live",
  polling: "Polling",
  refused: "Reopen URL",
};
const CONN_TITLE = {
  connecting: "Connecting to the live update channel.",
  live: "Live: callouts update without a reload.",
  polling: "Live updates are unavailable right now; falling back to checking for callouts every 1.5s.",
  refused: REFUSED_MESSAGE,
};

/**
 * Reads and consumes the per-run token from location.hash ("#t=<token>").
 * A fragment is never sent to a server, which is why the token travels
 * here rather than in the path or a query parameter for the initial page
 * load; it is promoted to a query parameter only for the one request that
 * needs it, the /ws handshake, because a browser cannot set headers on
 * that handshake and this is the only channel left that is not the
 * subprotocol field.
 *
 * The fragment is stripped from the visible URL unconditionally, whether
 * or not a token was found in it: a token left visible in the address bar
 * survives a reload, a bookmark and a screen share, all of which are wider
 * exposure than the "never sent to a server" property the fragment was
 * chosen for in the first place.
 */
function extractToken() {
  const hash = location.hash;
  let token = "";
  if (hash.startsWith("#")) {
    token = new URLSearchParams(hash.slice(1)).get("t") || "";
  }
  if (hash) {
    history.replaceState(null, "", location.pathname + location.search);
  }
  return token;
}

const TOKEN = extractToken();

function makeTabId() {
  if (window.crypto && typeof window.crypto.randomUUID === "function") {
    return window.crypto.randomUUID();
  }
  // Every browser this viewer targets has crypto.randomUUID; this only
  // covers an embedded WebView that stripped it. Uniqueness here only
  // needs to hold across the handful of tabs one person opens, not
  // cryptographic strength, since the tab id carries no authority: the
  // token is what proves who is allowed to connect at all.
  return "t" + Math.random().toString(36).slice(2) + Date.now().toString(36);
}

const TAB_ID = makeTabId();

function wsPath() {
  return "/ws?t=" + encodeURIComponent(TOKEN);
}

function wsUrl() {
  const scheme = location.protocol === "https:" ? "wss:" : "ws:";
  return scheme + "//" + location.host + wsPath();
}

export function initWs({
  startCalloutsPoll,
  stopCalloutsPoll,
  refetchCallouts,
  onHello = () => {},
  onAgentEvent = () => {},
}) {
  let ws = null;
  let opened = false; // true once this attempt's WebSocket has reached readyState OPEN
  let lastSeq = 0;
  let attempt = 0;
  let stopped = false; // permanently done trying: protocol mismatch or a confirmed 403
  let fallbackFired = false; // whether this downtime episode has already switched to polling
  let fallbackTimer = null;
  let reconnectTimer = null;
  let livenessTimer = null;

  const connIndicator = document.getElementById("connIndicator");
  function applyConnIndicator(state) {
    connIndicator.textContent = CONN_LABEL[state.connection] || state.connection;
    connIndicator.title = CONN_TITLE[state.connection] || "";
    connIndicator.dataset.state = state.connection;
  }
  applyConnIndicator(store.getState());
  store.subscribe("connection", applyConnIndicator);

  function backoffDelay(n) {
    const raw = Math.min(MAX_BACKOFF_MS, BASE_BACKOFF_MS * Math.pow(2, n));
    // +/-25% jitter, so a server restart does not get every tab's next
    // retry landing on it in the same instant.
    return raw * (0.75 + Math.random() * 0.5);
  }

  function send(frame) {
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(frame));
  }

  function clearLiveness() {
    clearTimeout(livenessTimer);
    livenessTimer = null;
  }

  function resetLiveness() {
    clearTimeout(livenessTimer);
    livenessTimer = setTimeout(handleLivenessExpiry, LIVENESS_TIMEOUT_MS);
  }

  function handleLivenessExpiry() {
    livenessTimer = null;
    if (stopped || !ws) return;
    // Nothing reset the deadline: this attempt's socket never delivered a
    // single message since connect(), whether because it never reached
    // OPEN, opened but never got a hello back, or went quiet sometime
    // after that with no close event of its own. ws.close() is the one
    // action that produces a decision in every one of those cases: fired
    // while still CONNECTING it closes without ever having opened, which
    // handleClose routes to probeRefusal; fired on an OPEN socket it
    // routes to scheduleRetry instead.
    try {
      ws.close();
    } catch (err) {
      // already closing or closed; handleClose was already called or is imminent
    }
  }

  function connect() {
    if (stopped) return;
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
    opened = false;
    try {
      ws = new WebSocket(wsUrl());
    } catch (err) {
      // Malformed URL or a browser that refuses to construct a WebSocket
      // (e.g. mixed content); nothing to learn from this beyond "try again
      // later", the same as any other pre-handshake failure.
      probeRefusal();
      return;
    }
    resetLiveness();
    ws.addEventListener("open", handleOpen);
    ws.addEventListener("message", handleMessage);
    ws.addEventListener("close", handleClose);
    // No separate handling in "error": the WebSocket spec fires close right
    // after error for every failure mode, and close is where readyState and
    // the close code are both available, so all decisions are made there.
    ws.addEventListener("error", () => {});
  }

  function handleOpen() {
    opened = true;
    send({
      v: PROTOCOL_VERSION,
      type: "hello",
      token: TOKEN,
      last_seq: lastSeq,
      viewer: { tab_id: TAB_ID, w: window.innerWidth, h: window.innerHeight },
    });
  }

  function handleMessage(event) {
    // Any inbound frame, parseable or not, proves the transport is still
    // alive; this is the one reset shared by the "never got a hello"
    // and "went idle after hello" halves of the liveness deadline.
    resetLiveness();
    let frame;
    try {
      frame = JSON.parse(event.data);
    } catch (err) {
      return; // not a frame this client understands; nothing was sent that depends on a reply
    }
    if (frame.v !== PROTOCOL_VERSION) {
      // The server is expected to close with 4400 rather than send a frame
      // with the wrong v to a client that announced the right one; this is
      // a defensive second check in case a future server build ever sends
      // one frame before closing.
      handleProtocolMismatch();
      return;
    }
    if (frame.type === "hello") {
      handleHello(frame);
    } else if (frame.type === "event") {
      handleEvent(frame);
    } else if (frame.type === "refused") {
      // Always sent in answer to something this page sent, and always
      // carrying a reason. Dropping it would leave the human watching for an
      // effect that is never coming, with the server's explanation of why
      // discarded one layer below the UI.
      toast(frame.reason || "the server refused that request", false);
    }
    // "call" and "ping": no handler yet. An unrecognised type from a
    // same-version server is simply a frame this build of the page has no
    // use for, not an error.
  }

  function handleHello(frame) {
    if (frame.protocol !== PROTOCOL_VERSION) {
      handleProtocolMismatch();
      return;
    }
    lastSeq = frame.seq;
    attempt = 0;
    fallbackFired = false;
    clearTimeout(fallbackTimer);
    fallbackTimer = null;
    store.setConnection("live");
    stopCalloutsPoll();
    // One refetch on every (re)connect, in addition to reacting to
    // callouts_changed below: replay only covers events still in the
    // server's 500-event ring, so a gap longer than that would otherwise
    // leave a stale callout list with no further event to prompt a refetch.
    refetchCallouts();
    onHello(frame.session);
  }

  function handleEvent(frame) {
    lastSeq = frame.seq;
    const kind = frame.event && frame.event.kind;
    if (kind === "callouts_changed") {
      refetchCallouts();
    } else {
      onAgentEvent(frame.event);
    }
  }

  function handleProtocolMismatch() {
    stopped = true;
    clearLiveness();
    clearTimeout(fallbackTimer);
    clearTimeout(reconnectTimer);
    if (ws) {
      try {
        ws.close();
      } catch (err) {
        // already closing or closed; nothing left to do
      }
    }
    showError(MISMATCH_MESSAGE);
    store.setConnection("polling");
    startCalloutsPoll();
  }

  function handleClose(event) {
    const wasOpened = opened;
    opened = false;
    ws = null;
    clearLiveness();
    if (stopped) return;
    if (wasOpened && event.code === 4400) {
      handleProtocolMismatch();
      return;
    }
    if (wasOpened) {
      // A drop after a successful handshake: the token, Origin and Host
      // checks already passed once for this page, so there is nothing to
      // diagnose, only a network blip, a laptop sleep or a server restart
      // to wait out.
      //
      // The state has to stop saying "live" here, before scheduleRetry arms
      // the fallback timer, because that timer's own guard skips the switch
      // to polling when it finds the connection live. Leaving it live across
      // a drop makes that guard read a state this close never cleared, and
      // the fallback never fires at all: the page then shows a live socket
      // and runs neither the push nor the poll, which is the exact outcome
      // the fallback exists to prevent.
      store.setConnection("connecting");
      scheduleRetry();
      return;
    }
    // Never reached "open". Per the WebSocket platform contract this looks
    // identical whether the cause was a wrong token, a rejected Origin or
    // Host, or the server simply being unreachable: every browser reports
    // a pre-handshake failure as the same reasonless abnormal closure, with
    // no status code and no body exposed to script. probeRefusal tells
    // a stale token apart from a transient outage by asking the same
    // question over plain HTTP instead, where the status code is visible.
    probeRefusal();
  }

  async function probeRefusal() {
    if (stopped) return;
    try {
      const res = await fetch(wsPath(), { cache: "no-store" });
      if (res.status === 403) {
        // Confirmed: this exact request, over a channel that does expose
        // its status, was refused before any upgrade was attempted. Per
        // the auth design, 403 on /ws is emitted by the token, Origin and
        // Host checks, which is the only way a request already reaching
        // this same origin fails here; further retries with the same
        // fixed token would repeat forever, so the socket path is
        // abandoned for this page load and the fallback poll takes over
        // for good.
        //
        // Only 403 is terminal, and every other status falls through to a
        // retry on purpose. A healthy server answers this same probe with
        // 400, because the probe is a plain GET carrying no upgrade headers
        // and the route reaches the handshake and rejects it there: that
        // status means the token was accepted and the socket failure was
        // transient, which is exactly the case worth retrying.
        stopped = true;
        clearTimeout(fallbackTimer);
        fallbackTimer = null;
        store.setConnection("refused");
        showError(REFUSED_MESSAGE);
        startCalloutsPoll();
        return;
      }
    } catch (err) {
      // The probe failed too (offline, DNS, TLS): no more information than
      // the WebSocket attempt already gave. Falls through to the ordinary
      // retry path below.
    }
    scheduleRetry();
  }

  function scheduleRetry() {
    if (stopped) return;
    if (!fallbackFired && fallbackTimer === null) {
      // Only armed once per downtime episode: repeated failures before it
      // fires must not stack a second timer that fires again later and
      // re-runs the switch-to-polling step redundantly.
      fallbackTimer = setTimeout(() => {
        fallbackTimer = null;
        if (stopped || fallbackFired || store.getState().connection === "live") return;
        fallbackFired = true;
        store.setConnection("polling");
        startCalloutsPoll();
      }, BASE_BACKOFF_MS);
    }
    reconnectTimer = setTimeout(connect, backoffDelay(attempt));
    attempt++;
  }

  connect();

  // `send` is the only capability this module hands back: chat.js's turn,
  // permission and interrupt frames all go out through it, so there is
  // still exactly one WebSocket and one place (this closure) that owns its
  // lifecycle, reconnects and replay bookkeeping.
  return { send };
}
