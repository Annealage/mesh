/**
 * The browser side of viewer control: the method table the server's `call`
 * frames are dispatched against, and the topbar pause control.
 *
 * Every entry here is the other half of one tool in `tools/viewer_tools.py`,
 * and the two files are a contract: the method names, the parameter names and
 * the shape of what a method returns are all read by that module. A change to
 * either side without the other shows up as a tool that reports success while
 * doing nothing, so the names are deliberately identical on both sides.
 *
 * Nothing here writes scene state directly. Every mutation goes through a
 * `store` mutator or through `three-scene.js`'s camera helpers, which is what
 * makes a tool call and a human's click the same operation: the parts
 * checkbox, the pin list and the mesh's own `visible` flag are all readers of
 * `store` state, so `set_visibility` from a tool moves the checkbox exactly as
 * a click on the checkbox moves the mesh.
 *
 * A failure is a `CommandError` carrying a code, which `ws.js` turns into the
 * protocol's `error` reply and the tool layer turns into a failed tool result
 * naming that code. Codes are for the model to act on: `unknown_model` means
 * "list the models again", `no_models_loaded` means "wait or generate one".
 *
 * The pause control lives here rather than in the chat pane because pausing is
 * about this viewer, not about the conversation, and because the thing it
 * gates is exactly the table above. It is a pure reader of `store.paused`:
 * clicking it sends a frame and nothing else, and the label only changes when
 * the server says the flag moved. A control that latched locally would show
 * "paused" while the tools it claims to gate were still running, which is the
 * defect the previous version of this control actually had.
 */

import { store } from "./store.js";

export class CommandError extends Error {
  constructor(code, message) {
    super(message);
    this.code = code;
  }
}

// Tried in order by `capture_view`, first result under MAX_CAPTURE_CHARS wins.
// PNG first because a shaded render of a part has hard edges and flat fields,
// which JPEG smears exactly where a model is being asked to judge a fillet;
// the JPEG rungs exist so a busy view still returns something rather than
// failing.
const CAPTURE_ENCODINGS = [
  ["png", undefined],
  ["jpeg", 0.92],
  ["jpeg", 0.8],
  ["jpeg", 0.6],
];

// Ceiling on the data URL a capture may return, in characters. This is the
// pair to `MAX_WS_MESSAGE` in http/ws.py (4 MiB) and must stay comfortably
// under it: an oversized inbound frame is not refused there, microdot raises
// and the connection drops, so the human's tab would reconnect with no
// explanation of why. Two thirds of the ceiling leaves room for the JSON the
// data URL is wrapped in and for a multi-byte character count that is not the
// same as a byte count.
export const MAX_CAPTURE_CHARS = 2 * 1024 * 1024;

// The largest capture this viewer will render, whatever is asked for. 1568 is
// the longest edge the API keeps for an image anyway, so anything above it
// costs encoding time and frame size for pixels the model never sees.
export const MAX_CAPTURE_WIDTH = 1568;

function round(v, d = 3) {
  const f = Math.pow(10, d);
  return Math.round(v * f) / f;
}

function readPoint(params, key) {
  const value = params[key];
  if (!Array.isArray(value) || value.length !== 3 || value.some((v) => typeof v !== "number" || !isFinite(v))) {
    throw new CommandError("bad_params", key + " must be three finite numbers, [x, y, z]");
  }
  return value;
}

/**
 * Resolve one `measure` reference to a point.
 *
 * The forms accepted are the ones the tool's own description promises:
 * `pin:3` or `u3` for one of the human's pins, `callout:2` or `a2` for an
 * agent callout, and `x,y,z` for a bare coordinate. The `u`/`a` spellings are
 * the same keys the Measure dropdowns use, so a human reading the panel and a
 * model reading this can name a pin the same way.
 *
 * Live store state, not the comments file: a pin the human has placed and
 * commented but not yet submitted is measurable here, and that is the whole
 * reason this is a viewer call rather than a file read.
 */
function resolveReference(ref, state) {
  const coords = ref.split(",");
  if (coords.length === 3) {
    const point = coords.map((c) => Number(c.trim()));
    if (point.every((v) => isFinite(v))) {
      return { ref, kind: "point", point };
    }
  }
  const match = /^(pin|u|callout|a)\s*[:# ]?\s*(\d+)$/i.exec(ref);
  if (!match) {
    throw new CommandError(
      "bad_reference",
      "cannot read " + JSON.stringify(ref) +
        " as a reference; use \"pin:3\", \"callout:2\" or \"12.5,-3.2,44\"",
    );
  }
  const wantsPin = /^(pin|u)$/i.test(match[1]);
  const id = Number(match[2]);
  if (wantsPin) {
    const pin = state.pins.find((p) => p.id === id);
    if (!pin) {
      const present = state.pins.map((p) => p.id).join(", ") || "none";
      throw new CommandError("unknown_pin",
        "there is no pin " + id + " in the viewer; the pins present are: " + present);
    }
    return { ref, kind: "pin", id, part: pin.part, label: pin.label, point: pin.point };
  }
  const callouts = state.callouts;
  const index = callouts.findIndex((c, i) => (c.id != null ? c.id : i + 1) === id);
  if (index === -1) {
    const present = callouts.map((c, i) => (c.id != null ? c.id : i + 1)).join(", ") || "none";
    throw new CommandError("unknown_callout",
      "there is no callout " + id + " in the viewer; the callouts present are: " + present);
  }
  const callout = callouts[index];
  return {
    ref,
    kind: "callout",
    id,
    part: callout.part,
    label: callout.label,
    point: callout.point || [0, 0, 0],
  };
}

/**
 * @param scene3d  the object three-scene.js's initScene returned
 * @param send     ws.js's frame sender, for the outbound pause frame
 */
export function initCommands({ scene3d, send }) {
  const pauseBtn = document.getElementById("pauseBtn");

  function partsList() {
    const state = store.getState();
    return state.models.map((m) => ({
      rel: m.rel,
      label: m.label,
      visible: !!state.visibility[m.rel],
    }));
  }

  const methods = {
    "viewer.get_view": () => scene3d.cameraState(),

    "viewer.set_view": (params) => {
      const move = {};
      if (params.position !== undefined) move.position = readPoint(params, "position");
      if (params.target !== undefined) move.target = readPoint(params, "target");
      if (params.up_axis !== undefined) {
        if (params.up_axis !== "z" && params.up_axis !== "y") {
          throw new CommandError("bad_params", "up_axis must be \"z\" or \"y\"");
        }
        // Through the store, so the up-axis button's label, `camera.up` and
        // the refit that follows all happen the one way they happen for a
        // human's click. Applied before the camera move, since the store's
        // own subscriber refits, which would otherwise discard the move.
        store.setUpAxis(params.up_axis);
      }
      if (move.position || move.target) return scene3d.setView(move);
      return scene3d.cameraState();
    },

    "viewer.fit_view": () => {
      // fitView reports whether a mesh was both loaded and visible, which is
      // the honest answer: with nothing to frame the camera does not move, and
      // a success here would tell the model the view is now showing something.
      if (!scene3d.fitView()) {
        throw new CommandError("nothing_visible",
          "no part is loaded and visible, so there was nothing to frame; show " +
          "one with set_visibility, or wait if a part is still being written");
      }
      return scene3d.cameraState();
    },

    "viewer.get_visibility": () => ({ parts: partsList() }),

    "viewer.set_visibility": (params) => {
      const model = store.getState().models.find((m) => m.rel === params.rel);
      if (!model) {
        const known = store.getState().models.map((m) => m.rel).join(", ") || "none";
        throw new CommandError(
          store.getState().models.length ? "unknown_model" : "no_models_loaded",
          "the viewer has no part at rel " + JSON.stringify(params.rel) +
            "; the parts loaded are: " + known,
        );
      }
      store.setVisibility(params.rel, !!params.visible);
      return { rel: model.rel, label: model.label, visible: !!params.visible };
    },

    "viewer.set_up_axis": (params) => {
      if (params.axis !== "z" && params.axis !== "y") {
        throw new CommandError("bad_params", "axis must be \"z\" or \"y\"");
      }
      store.setUpAxis(params.axis);
      return scene3d.cameraState();
    },

    "viewer.select_pin": (params) => {
      const pin = store.getState().pins.find((p) => p.id === params.pin);
      if (!pin) {
        const present = store.getState().pins.map((p) => p.id).join(", ") || "none";
        throw new CommandError("unknown_pin",
          "there is no pin " + params.pin + " in the viewer; the pins present " +
          "are: " + present + ". A pin the human deleted, or one from a session " +
          "before this page was loaded, is not there even if it is in " +
          "mesh-comments.json");
      }
      store.selectPin(pin.id);
      return { pin: pin.id, part: pin.part, label: pin.label, point: pin.point,
               comment: pin.comment };
    },

    "viewer.capture_view": (params) => {
      const canvas = scene3d.renderer.domElement;
      if (!canvas.width || !canvas.height) {
        // Nothing has ever sized this renderer, so there is no drawing buffer
        // to read and the width arithmetic below would divide by zero.
        throw new CommandError("no_canvas",
          "the 3D canvas has no size yet, so there is nothing to capture");
      }
      let width = params.width;
      if (width !== undefined) {
        if (typeof width !== "number" || !isFinite(width)) {
          throw new CommandError("bad_params", "width must be a number of pixels");
        }
        width = Math.max(64, Math.min(MAX_CAPTURE_WIDTH, Math.round(width)));
      }
      const shot = scene3d.captureView({
        width,
        encodings: CAPTURE_ENCODINGS,
        maxChars: MAX_CAPTURE_CHARS,
      });
      if (!shot) {
        throw new CommandError("capture_too_large",
          "the capture does not fit in one frame even as JPEG; ask for a " +
          "smaller width");
      }
      return { ...shot, camera: scene3d.cameraState() };
    },

    "viewer.measure": (params) => {
      const state = store.getState();
      const a = resolveReference(String(params.a), state);
      const b = resolveReference(String(params.b), state);
      const delta = [0, 1, 2].map((i) => round(b.point[i] - a.point[i]));
      const distance = round(Math.hypot(delta[0], delta[1], delta[2]));
      return { a, b, delta, distance };
    },
  };

  /**
   * Run one server `call` and return its result.
   *
   * Async even though every method above is synchronous, so a method that
   * later needs to await something (a fetch, a decode) needs no change here
   * and no change in ws.js.
   */
  async function dispatch(method, params) {
    const handler = methods[method];
    if (!handler) {
      throw new CommandError("unknown_method",
        "this viewer does not implement " + method + "; it may be running an " +
        "older build than the server");
    }
    return handler(params || {});
  }

  function renderPause(state) {
    pauseBtn.classList.toggle("on", state.paused);
    pauseBtn.textContent = state.paused ? "❙❙ Paused" : "❙❙ Pause";
    pauseBtn.setAttribute("aria-pressed", state.paused ? "true" : "false");
    // Disabled when there is no agent, which is both viewer-only mode and a
    // session that failed to start. In viewer-only mode the server refuses a
    // pause frame outright, since there are no tools to pause, and an enabled
    // control whose every click produces a refusal toast is worse than one that
    // plainly cannot be pressed. Keyed on the same status the composer's Send
    // button reads, so the pane and the topbar agree about what is available.
    pauseBtn.disabled = state.chat.agentStatus === "unavailable";
  }
  renderPause(store.getState());
  store.subscribe("paused", renderPause);
  store.subscribe("chat", renderPause);

  pauseBtn.addEventListener("click", () => {
    // The current state is inverted and sent; the button's own appearance
    // does not change until the server broadcasts that the flag moved. That
    // is deliberate: the flag lives in the server because the tools it gates
    // run there, so a local latch would be a claim this page cannot make.
    send({ v: 1, type: "pause", paused: !store.getState().paused });
  });

  return { dispatch, setPausedFromServer: (paused) => store.setPaused(paused) };
}
