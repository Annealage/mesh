/**
 * The chat pane: composer, streamed transcript, tool cards, permission
 * cards, interrupt and pause. The store stays the single writer (M3's
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
  const pauseToggleEl = document.getElementById("pauseToggle");

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
    rec.inputEl.textContent = safeJson(tool.input);
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

    function decide(decision) {
      const message = decision === "deny" ? reasonEl.value : "";
      send({ v: 1, type: "permission", request_id: req.request_id, decision, message });
      store.removeChatPermissionRequest(req.request_id);
    }
    allowBtn.addEventListener("click", () => decide("allow"));
    allowAlwaysBtn.addEventListener("click", () => decide("allow_always"));
    denyBtn.addEventListener("click", () => decide("deny"));

    actions.appendChild(allowBtn);
    actions.appendChild(allowAlwaysBtn);
    actions.appendChild(reasonEl);
    actions.appendChild(denyBtn);

    card.appendChild(toolEl);
    card.appendChild(inputEl);
    card.appendChild(actions);

    return { card, toolEl, inputEl };
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
      rec.inputEl.textContent = safeJson(req.input);
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

  function renderPauseToggle(chat) {
    pauseToggleEl.checked = chat.paused;
  }

  function render() {
    const chat = store.getState().chat;
    renderTurns(chat);
    renderPending(chat);
    renderAgentStatus(chat);
    renderBanner(chat);
    renderInterrupt(chat);
    renderPauseToggle(chat);
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

  // `paused` has no accepting slot in protocol.py's inbound frame catalogue
  // yet: `_check_state` only allows {camera, visibility, selection, mode}
  // inside a `state` frame, `AgentSession`'s own docstring says a `state`
  // frame is deliberately not agent state in the first place, and no
  // `pause` type exists in `_INBOUND_SPECS` either. This sends the shape
  // the permission broker will need a slot for; until one exists the
  // server answers with a `refused` frame this client does not yet parse
  // (see ws.js's `handleMessage`), so the checkbox changes locally but has
  // no server-side effect until that slot is added.
  pauseToggleEl.addEventListener("change", () => {
    store.setChatPaused(pauseToggleEl.checked);
    send({ v: 1, type: "pause", paused: pauseToggleEl.checked });
  });

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
