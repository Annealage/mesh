/**
 * The Workbench frame's own indicators, driven by state the agent layer keeps
 * on the chat pane rather than by a second event channel.
 *
 * chat.js keeps `data-working`, `data-turn-started` (epoch ms, when this page
 * first saw the turn) and `data-pending` (open approvals) on `#chat`, and
 * `data-state` plus a label on `#agentStatus`. This module mirrors those into
 * the chat header's working timer and the status bar, and shows the status
 * bar's approval alert, which jumps to the first pending card.
 */

import { store } from "./store.js";

function elapsed(ms) {
  const s = Math.max(0, Math.floor(ms / 1000));
  const m = Math.floor(s / 60);
  return m + ":" + String(s % 60).padStart(2, "0");
}

export function initChrome() {
  const chat = document.getElementById("chat");
  const agentStatus = document.getElementById("agentStatus");
  const chatTime = document.getElementById("chatWorkingTime");
  const sbAgent = document.getElementById("sbAgent");
  const sbWorking = document.getElementById("sbWorking");
  const sbTime = document.getElementById("sbWorkingTime");
  const alert = document.getElementById("approvalAlert");
  const alertText = document.getElementById("approvalAlertText");
  const pending = document.getElementById("chatPending");

  let timer = null;

  function tick() {
    const started = Number(chat.dataset.turnStarted);
    const text = started ? elapsed(Date.now() - started) : "";
    chatTime.textContent = text;
    sbTime.textContent = text;
  }

  function syncWorking() {
    const working = "working" in chat.dataset;
    sbWorking.hidden = !working;
    if (working && !timer) {
      tick();
      timer = setInterval(tick, 1000);
    } else if (!working && timer) {
      clearInterval(timer);
      timer = null;
    }
  }

  function syncPending() {
    const count = Number(chat.dataset.pending) || 0;
    alert.hidden = count === 0;
    if (!count) return;
    const card = pending.querySelector(".permcard .ptool");
    const tool = card ? card.textContent : "";
    alertText.textContent =
      (count === 1 ? "Approval needed" : count + " approvals needed") + (tool ? ": " + tool : "");
  }

  function syncStatus() {
    sbAgent.textContent = agentStatus.textContent;
  }

  new MutationObserver(() => {
    syncWorking();
    syncPending();
  }).observe(chat, { attributes: true, attributeFilter: ["data-working", "data-turn-started", "data-pending"] });
  new MutationObserver(syncStatus).observe(agentStatus, { childList: true, characterData: true, subtree: true });
  syncWorking();
  syncPending();
  syncStatus();

  alert.addEventListener("click", () => {
    const card = pending.querySelector(".permcard");
    if (!card) return;
    // The chat pane is its own tab at phone width.
    const tab = document.querySelector('#tabbar button[data-tab="chat"]');
    if (tab && getComputedStyle(tab).display !== "none") tab.click();
    card.scrollIntoView({ block: "nearest" });
    const allow = card.querySelector(".pactions button");
    if (allow) allow.focus();
  });

  // The Measure tool opens the review panel on its Measure section.
  document.getElementById("measureTool").addEventListener("click", () => {
    const sec = document.getElementById("measureSec");
    if (!store.getState().panelOpen) document.getElementById("panelBtn").click();
    sec.open = true;
    sec.scrollIntoView({ block: "nearest" });
    document.getElementById("mA").focus();
  });
}
