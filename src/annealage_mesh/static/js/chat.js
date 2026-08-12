/**
 * The chat pane: composer, streamed transcript, tool cards, permission
 * cards and interrupt. The store stays the single writer (M3's
 * contract); this module only reads `store.getState().chat` and calls the
 * `chat*` mutators store.js exports, and only builds DOM.
 *
 * Model text is never inserted as HTML. Every value that came from the
 * agent or from a tool, a tool name, a tool's JSON input, a tool's result
 * text, a permission request's fields, a banner's text, is written with
 * `.textContent`, which cannot be interpreted as markup no matter what it
 * contains. The one exception is the assistant's own free-form turn text,
 * which is allowed a small, fixed set of inline styles; `renderModelText`
 * below is the only place this module ever assigns `.innerHTML`, and it
 * only ever does so after `escapeHtml` has run over the raw string first,
 * so every character that could open a tag has already become an entity
 * before any of the markdown-style regexes see it. Each regex wraps an
 * already-escaped substring in a fixed literal tag; none of them re-reads
 * or re-matches text a previous one inserted, so there is no path back
 * from "escaped text" to "text a later step interprets as markup".
 *
 * Reconciliation follows pins.js's pattern: a Map keyed by a stable id
 * (turn number, tool_use_id, permission request_id) so a re-render updates
 * an existing element in place rather than replacing it, which is what
 * keeps a `<details>` tool card's open/closed state and the composer's
 * focus and caret position untouched by an unrelated event arriving.
 */

import { store } from "./store.js";
import { toast } from "./ui.js";

// What a card shows while its decision is in flight, keyed by the decision
// this view sent.
const DECISION_SENT = Object.freeze({
  allow: "Allow sent…",
  allow_always: "Always allow sent…",
  deny: "Deny sent…",
});

// How a resolution reads in a toast. The three that nobody clicked are worth
// naming precisely: a card that vanishes because it expired looks, without
// this, exactly like one somebody answered.
const OUTCOME_TEXT = Object.freeze({
  allow: "was allowed",
  allow_always: "was allowed for the rest of this session",
  deny: "was denied",
  timeout: "expired before it was answered",
  no_viewer: "was cancelled when every view disconnected",
  shutdown: "was cancelled by shutdown",
});

// The outcomes that mean a person clicked something, as opposed to the request
// running out of time or of viewers.
const HUMAN_DECISIONS = new Set(["allow", "allow_always", "deny"]);

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

// Applies the non-fence inline styles (inline code, bold, italic, newline)
// to a string that has already been through escapeHtml. Order matters:
// inline code runs first so a literal `*` or `**` inside a code span is
// never read as emphasis, and bold runs before italic so `**x**` is not
// left as an unmatched pair of single asterisks once the double-asterisk
// match is gone.
function renderInline(escaped) {
  return escaped
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/\*([^*]+)\*/g, "<em>$1</em>")
    .replace(/\n/g, "<br>");
}

/**
 * Escapes `text`, then applies a fixed, small set of transforms: fenced
 * code blocks become `<pre>`, and within the rest, inline code, bold,
 * italic and newlines get their usual HTML equivalents. This is a
 * hand-written renderer over already-escaped text, not a markdown parser
 * and not `innerHTML` of the model's own output; see this module's header
 * comment for why that distinction is the whole point of it.
 */
function renderModelText(text) {
  const escaped = escapeHtml(text);
  const fenceRe = /```[^\n`]*\n([\s\S]*?)```/g;
  const segments = [];
  let lastIndex = 0;
  let m;
  while ((m = fenceRe.exec(escaped))) {
    segments.push({ code: false, text: escaped.slice(lastIndex, m.index) });
    segments.push({ code: true, text: m[1] });
    lastIndex = m.index + m[0].length;
  }
  segments.push({ code: false, text: escaped.slice(lastIndex) });
  return segments
    .map((seg) => (seg.code ? '<pre class="code">' + seg.text + "</pre>" : renderInline(seg.text)))
    .join("");
}

// Ordinary Claude Code tools (Bash, Edit, Write, ...) keep their plain
// name; an in-process MCP tool arrives as `mcp__<server>__<tool>` and is
// shown as `<server>: <tool>` instead of the wire form.
function shortToolName(name) {
  const m = /^mcp__([^_]+)__(.+)$/.exec(name);
  return m ? m[1] + ": " + m[2] : name;
}

function safeJson(value) {
  try {
    return JSON.stringify(value, null, 2);
  } catch (err) {
    return String(value);
  }
}

// A tool's arguments, laid out to be read rather than parsed. JSON alone is the
// wrong shape for the arguments that matter most here: a Bash command is a
// script, and stringifying it turns every line break into a literal \n, so the
// one field the human most needs to inspect before approving it arrives as a
// single unreadable line. String values are therefore printed as themselves,
// with their newlines intact; anything else falls back to JSON.
//
// The result is always assigned with textContent, never innerHTML: these values
// come from the model.
function formatToolInput(input) {
  if (input === null || input === undefined) return "";
  if (typeof input !== "object") return String(input);
  const keys = Object.keys(input);
  if (!keys.length) return "";
  // The overwhelmingly common shape, and the one where a key label adds
  // nothing: one string argument, such as a command or a path.
  if (keys.length === 1 && typeof input[keys[0]] === "string") {
    return input[keys[0]];
  }
  return keys
    .map((key) => {
      const value = input[key];
      const shown = typeof value === "string" ? value : safeJson(value);
      return shown.includes("\n") ? key + ":\n" + shown : key + ": " + shown;
    })
    .join("\n");
}

// The composer only ever builds {type: "text", text} blocks (protocol.py's
// _BLOCK_SPECS also allows image_path, but there is no upload control in
// this pane), so this only needs to join the text blocks a `turn` record's
// `user` field carries.
function blocksToText(blocks) {
  if (!blocks) return "";
  return blocks
    .filter((b) => b.type === "text")
    .map((b) => b.text)
    .join("\n\n");
}

const AGENT_LABEL = { connecting: "Connecting…", ready: "Ready", unavailable: "Unavailable" };
const AGENT_TITLE = {
  connecting: "The agent process is starting.",
  ready: "The agent is ready to receive a message.",
  unavailable:
    "The agent is not available right now. The viewer, pins and Submit keep working regardless.",
};

export function initChat({ send }) {
  const chatLogEl = document.getElementById("chatLog");
  const chatPendingEl = document.getElementById("chatPending");
  const agentStatusEl = document.getElementById("agentStatus");
  const bannerEl = document.getElementById("chatBanner");
  const bannerTextEl = document.getElementById("chatBannerText");
  const bannerCloseBtn = document.getElementById("chatBannerClose");
  const chatInputEl = document.getElementById("chatInput");
  const chatSendBtn = document.getElementById("chatSend");
  const chatInterruptBtn = document.getElementById("chatInterrupt");

  // turn number -> {row, textEl, toolsEl, metaEl, userEl, tools: Map<tool_use_id, {card, resultEl}>}
  const turnEls = new Map();
  // request_id -> element
  const pendingEls = new Map();

  let stickToBottom = true;
  chatLogEl.addEventListener("scroll", () => {
    stickToBottom =
      chatLogEl.scrollTop + chatLogEl.clientHeight >= chatLogEl.scrollHeight - 24;
  });

  function buildToolCard(tool) {
    const card = document.createElement("details");
    card.className = "toolcard";
    card.dataset.toolId = tool.tool_use_id;
    const summary = document.createElement("summary");
    const inputEl = document.createElement("pre");
    inputEl.className = "toolinput";
    const resultEl = document.createElement("pre");
    resultEl.className = "toolresult";
    resultEl.hidden = true;
    card.appendChild(summary);
    card.appendChild(inputEl);
    card.appendChild(resultEl);
    return { card, summary, inputEl, resultEl };
  }

  function updateToolCard(rec, tool) {
    rec.summary.textContent = shortToolName(tool.name);
    rec.inputEl.textContent = formatToolInput(tool.input);
    if (tool.result) {
      rec.resultEl.hidden = false;
      rec.resultEl.textContent = tool.result.text;
      rec.resultEl.classList.toggle("error", !!tool.result.isError);
    } else {
      rec.resultEl.hidden = true;
    }
  }

  function buildTurnRow(turn) {
    const row = document.createElement("div");
    row.className = "turn";
    row.dataset.turn = String(turn);

    const userEl = document.createElement("div");
    userEl.className = "msg user";
    userEl.hidden = true;
    const userText = document.createElement("div");
    userText.className = "text";
    userEl.appendChild(userText);

    const assistantEl = document.createElement("div");
    assistantEl.className = "msg assistant";
    const textEl = document.createElement("div");
    textEl.className = "text";
    const toolsEl = document.createElement("div");
    toolsEl.className = "tools";
    const metaEl = document.createElement("div");
    metaEl.className = "turnmeta";
    assistantEl.appendChild(textEl);
    assistantEl.appendChild(toolsEl);
    assistantEl.appendChild(metaEl);

    row.appendChild(userEl);
    row.appendChild(assistantEl);

    return { row, userEl, userText, textEl, toolsEl, metaEl, tools: new Map() };
  }

  function updateTurnRow(rec, t) {
    if (t.user) {
      rec.userEl.hidden = false;
      rec.userText.textContent = blocksToText(t.user);
    }
    rec.textEl.innerHTML = renderModelText(t.text);

    const seenTools = new Set();
    t.tools.forEach((tool) => {
      seenTools.add(tool.tool_use_id);
      let toolRec = rec.tools.get(tool.tool_use_id);
      if (!toolRec) {
        toolRec = buildToolCard(tool);
        rec.tools.set(tool.tool_use_id, toolRec);
        rec.toolsEl.appendChild(toolRec.card);
      }
      updateToolCard(toolRec, tool);
    });
    for (const [id, toolRec] of rec.tools) {
      if (!seenTools.has(id)) {
        toolRec.card.remove();
        rec.tools.delete(id);
      }
    }

    if (t.complete) {
      rec.metaEl.textContent = "Stop: " + t.stopReason + " · $" + t.costUsd.toFixed(4);
    } else {
      rec.metaEl.textContent = "";
    }
  }

  function renderTurns(chat) {
    const seen = new Set();
    chat.turns.forEach((t) => {
      seen.add(t.turn);
      let rec = turnEls.get(t.turn);
      if (!rec) {
        rec = buildTurnRow(t.turn);
        turnEls.set(t.turn, rec);
        chatLogEl.appendChild(rec.row);
      }
      updateTurnRow(rec, t);
    });
    for (const [turn, rec] of turnEls) {
      if (!seen.has(turn)) {
        rec.row.remove();
        turnEls.delete(turn);
      }
    }
    if (stickToBottom) chatLogEl.scrollTop = chatLogEl.scrollHeight;
  }

  function buildPermissionCard(req) {
    const card = document.createElement("div");
    card.className = "permcard";
    card.dataset.requestId = req.request_id;

    const toolEl = document.createElement("div");
    toolEl.className = "ptool";
    const inputEl = document.createElement("pre");
    inputEl.className = "toolinput";

    const actions = document.createElement("div");
    actions.className = "pactions";
    const allowBtn = document.createElement("button");
    allowBtn.type = "button";
    allowBtn.textContent = "Allow";
    const allowAlwaysBtn = document.createElement("button");
    allowAlwaysBtn.type = "button";
    allowAlwaysBtn.textContent = "Always allow";
    const reasonEl = document.createElement("textarea");
    reasonEl.className = "preason";
    reasonEl.placeholder = "Reason (sent to the agent if you deny)";
    reasonEl.rows = 1;
    const denyBtn = document.createElement("button");
    denyBtn.type = "button";
    denyBtn.className = "danger";
    denyBtn.textContent = "Deny";

    // The card is not removed here. A decision is a request to the server, not
    // the answer: another view may already have answered this card, or it may
    // have expired, in which case this click decides nothing. Removing it now
    // would render those cases identically to a decision that took effect,
    // which for a Deny the human believes they made is the worst of the three.
    // `permission_resolved` retires the card, and carries the outcome that
    // actually applied.
    function decide(decision) {
      const message = decision === "deny" ? reasonEl.value : "";
      send({ v: 1, type: "permission", request_id: req.request_id, decision, message });
      store.markChatPermissionSubmitted(req.request_id, decision);
    }
    allowBtn.addEventListener("click", () => decide("allow"));
    allowAlwaysBtn.addEventListener("click", () => decide("allow_always"));
    denyBtn.addEventListener("click", () => decide("deny"));

    actions.appendChild(allowBtn);
    actions.appendChild(allowAlwaysBtn);
    actions.appendChild(reasonEl);
    actions.appendChild(denyBtn);

    const statusEl = document.createElement("div");
    statusEl.className = "pstatus";

    card.appendChild(toolEl);
    card.appendChild(inputEl);
    card.appendChild(actions);
    card.appendChild(statusEl);

    const buttons = [allowBtn, allowAlwaysBtn, denyBtn];
    return { card, toolEl, inputEl, statusEl, buttons, reasonEl };
  }

  // Tells the human when a card ended in a way they would otherwise have to
  // guess at: a decision this view sent that did not apply, and a resolution
  // nobody chose. A card another view answered while this one was only
  // watching just disappears, which is what a second device answering is
  // supposed to look like.
  function reportResolution(event) {
    const req = store.getState().chat.pending.find(
      (p) => p.request_id === event.request_id,
    );
    if (!req) return;
    const what = OUTCOME_TEXT[event.outcome] || ("ended as " + event.outcome);
    const tool = shortToolName(req.tool);
    if (req.submitted && req.submitted !== event.outcome) {
      toast(tool + ": your decision did not apply, the request " + what, false);
    } else if (!req.submitted && !HUMAN_DECISIONS.has(event.outcome)) {
      toast(tool + ": the request " + what, false);
    }
  }

  function renderPending(chat) {
    const seen = new Set();
    chat.pending.forEach((req) => {
      seen.add(req.request_id);
      let rec = pendingEls.get(req.request_id);
      if (!rec) {
        rec = buildPermissionCard(req);
        pendingEls.set(req.request_id, rec);
        chatPendingEl.appendChild(rec.card);
      }
      rec.toolEl.textContent = shortToolName(req.tool);
      rec.inputEl.textContent = formatToolInput(req.input);
      // A card whose decision is in flight stays visible and stops accepting
      // clicks, so a second click cannot send a second decision for one
      // request and the human can see which answer is on its way.
      const submitted = req.submitted || "";
      rec.buttons.forEach((b) => { b.disabled = !!submitted; });
      rec.reasonEl.disabled = !!submitted;
      rec.card.classList.toggle("submitted", !!submitted);
      rec.statusEl.textContent = submitted ? DECISION_SENT[submitted] : "";
    });
    for (const [id, rec] of pendingEls) {
      if (!seen.has(id)) {
        rec.card.remove();
        pendingEls.delete(id);
      }
    }
  }

  function renderAgentStatus(chat) {
    agentStatusEl.textContent = AGENT_LABEL[chat.agentStatus] || chat.agentStatus;
    agentStatusEl.title = AGENT_TITLE[chat.agentStatus] || "";
    agentStatusEl.dataset.state = chat.agentStatus;
    chatSendBtn.disabled = chat.agentStatus === "unavailable";
  }

  function renderBanner(chat) {
    if (chat.banner) {
      bannerEl.hidden = false;
      bannerEl.dataset.kind = chat.banner.kind;
      bannerTextEl.textContent = chat.banner.text;
    } else {
      bannerEl.hidden = true;
    }
  }

  function renderInterrupt(chat) {
    const busy = chat.pendingUser.length > 0 || chat.turns.some((t) => !t.complete);
    chatInterruptBtn.disabled = !busy;
  }

  function render() {
    const chat = store.getState().chat;
    renderTurns(chat);
    renderPending(chat);
    renderAgentStatus(chat);
    renderBanner(chat);
    renderInterrupt(chat);
  }

  store.subscribe("chat", render);
  render();

  function doSend() {
    const text = chatInputEl.value.trim();
    if (!text) return;
    const blocks = [{ type: "text", text }];
    store.queueChatUserTurn(blocks);
    send({ v: 1, type: "turn", blocks });
    chatInputEl.value = "";
  }

  chatSendBtn.addEventListener("click", doSend);
  chatInputEl.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      doSend();
    }
  });

  chatInterruptBtn.addEventListener("click", () => {
    send({ v: 1, type: "interrupt" });
  });

  bannerCloseBtn.addEventListener("click", () => store.clearChatBanner());

  function handleHello(session) {
    if (session) store.setChatAgentStatus(session.agent);
  }

  function handleEvent(event) {
    if (!event) return;
    switch (event.kind) {
      case "text_delta":
        store.appendChatTextDelta(event.turn, event.text);
        break;
      case "tool_use":
        store.addChatToolUse(event.turn, event.tool_use_id, event.name, event.input);
        break;
      case "tool_result":
        store.setChatToolResult(event.tool_use_id, !!event.is_error, event.text);
        break;
      case "turn_end":
        store.endChatTurn(event.turn, event.stop_reason, event.cost_usd);
        break;
      case "permission_request":
        store.addChatPermissionRequest(
          event.request_id,
          event.tool,
          event.input,
          event.suggestions,
        );
        break;
      case "permission_resolved":
        reportResolution(event);
        store.removeChatPermissionRequest(event.request_id);
        break;
      case "agent_status":
        // The hello frame's `session.agent` is only the status at the moment
        // this connection was accepted; this is what keeps it current.
        store.setChatAgentStatus(event.status);
        break;
      case "session_reset":
        store.resetChatTurns();
        store.setChatBanner("reset", event.reason);
        break;
      case "agent_error":
        store.setChatBanner("error", event.remediation || event.stderr);
        break;
      default:
        // "viewer_primary" and any future kind: no rendering in this pane yet.
        break;
    }
  }

  return { handleHello, handleEvent };
}
