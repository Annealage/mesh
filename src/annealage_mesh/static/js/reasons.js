/**
 * Says why a control cannot be used. The agent layer disables Send, Pause,
 * Interrupt and the model field when it cannot act but leaves the tooltip to
 * name the action; this runs after its own render (it subscribes later) and
 * puts the reason in the title, and takes the message box and Attach out of
 * reach when there is no agent to read what is typed.
 */
import { store } from "agent/store.js";

const NO_AGENT = "The agent is not running. Pins and Submit still work.";

export function initReasons() {
  const $ = (id) => document.getElementById(id);
  const send = $("chatSend"), input = $("chatInput"), attach = $("chatAttachBtn");
  const interrupt = $("chatInterrupt"), model = $("chatModelInput"), pause = $("pauseBtn");
  const ownDisabled = new Set(); // controls this module turned off

  // Writes only a changed value: an unchanged write still queues a mutation
  // record, and the observer below would answer it forever.
  const put = (el, prop, value) => { if (el[prop] !== value) el[prop] = value; };

  function lock(el, off) {
    if (!el) return;
    if (off) {
      put(el, "disabled", true);
      ownDisabled.add(el);
    } else if (ownDisabled.has(el)) {
      put(el, "disabled", false);
      ownDisabled.delete(el);
    }
  }

  function render(state) {
    const chat = state.chat;
    const gone = chat.agentStatus === "unavailable";
    lock(send, gone);
    lock(input, gone);
    lock(attach, gone);
    if (gone) {
      put(send, "title", NO_AGENT);
      put(input, "title", NO_AGENT);
      put(attach, "title", NO_AGENT);
    } else if (attach) {
      put(input, "title", "");
      put(attach, "title", "Attach an image (or paste or drop one into the message box)");
    }
    if (interrupt.disabled) {
      put(interrupt, "title", gone ? NO_AGENT : "No turn is running");
    } else {
      put(interrupt, "title", "Stop the current turn");
    }
    put(model, "title", gone ? NO_AGENT : "Switch the model for this conversation, live, with no restart");
    if (pause.disabled) {
      put(pause, "title", NO_AGENT);
    } else {
      put(pause, "title", "Pause the agent's control of this viewer. While paused, its tools that move the camera, show or hide parts, select a pin or add a callout are refused.");
    }
  }

  // The agent layer rewrites Send's state on its own renders, which can land
  // after this one; watch the button and put the reason back if it does.
  let settling = false;
  new MutationObserver(() => {
    if (settling) return;
    settling = true;
    render(store.getState());
    settling = false;
  }).observe(send, { attributes: true, attributeFilter: ["disabled", "title"] });

  render(store.getState());
  store.subscribe("chat", render);
  store.subscribe("paused", render);
}
