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
 *
 * Attachment chips follow the same reconciliation pattern, keyed by the
 * store's own attachment id rather than by `path`, because a chip exists (in
 * its "uploading" state) before an upload has produced a path at all. Every
 * chip is drawn from `state.chat.attachments`, whatever state the entry is
 * in, so this module keeps no second record of what is attached and cannot
 * disagree with the one the cap and Send both read. `uploadImage` is the one
 * function behind all three ways an image enters the composer: the file
 * picker, a paste onto the textarea, and a drop onto the chat pane. A sent
 * turn's own thumbnails are a second, simpler case: built once from
 * `record.user`'s `image_path` blocks and never reconciled again, since a
 * turn's composer blocks do not change after the turn is sent.
 */

import { store } from "./store.js";
import { uploadImage } from "./uploads.js";
import { toast } from "./ui.js";

// The three image types the upload route accepts (paths.py's
// `_IMAGE_NAME_RE`/`sniff_image`) and the same byte cap it enforces
// (`paths.MAX_IMAGE_BYTES`). Checking both here means a wrong-typed file or
// an oversized drop is refused before a single byte leaves the page, rather
// than after the whole body has streamed to the server only to be refused
// there. A file this accepts may still be too large to send to the model
// inline, which the server decides and reports per attachment; it is stored
// and named either way, so that is not a reason to refuse it here.
const ACCEPTED_IMAGE_TYPES = ["image/png", "image/jpeg", "image/webp"];
const MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024;

function localAttachmentRefusal(file) {
  if (!ACCEPTED_IMAGE_TYPES.includes(file.type)) {
    return file.name + ": only PNG, JPEG or WEBP images can be attached";
  }
  if (file.size > MAX_ATTACHMENT_BYTES) {
    return file.name + " is larger than the 8 MB upload limit";
  }
  return null;
}

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

// Joins the text blocks of a turn's composer blocks for the bubble's text;
// the same blocks' `image_path` entries are rendered separately, as
// thumbnails, by renderUserAttachments below.
function blocksToText(blocks) {
  if (!blocks) return "";
  return blocks
    .filter((b) => b.type === "text")
    .map((b) => b.text)
    .join("\n\n");
}

// An `image_path` block's `path` is "images/<name>" (protocol.py's block
// shape, one directory component); the image itself is served one
// component further in, at "/asset/<name>".
function assetUrlForImagePath(path) {
  const name = path.startsWith("images/") ? path.slice("images/".length) : path;
  return "/asset/" + name;
}

// Builds a sent turn's own thumbnail row from its composer blocks, once:
// `container` starts empty and this only ever appends, since `record.user`
// is set once (ensureTurn) and never changes afterwards. This is why a sent
// turn's images come from the local record rather than from the agent's
// echo of the turn: the SDK's message parser drops an image block out of
// that echo with no error, so the echo cannot be relied on to show what was
// actually sent, while the composer's own blocks already are that answer.
// Every element is built with createElement and given its `src` directly;
// this pane never assigns HTML from a value that did not originate here.
function renderUserAttachments(container, blocks) {
  if (container.childElementCount || !blocks) return;
  blocks
    .filter((b) => b.type === "image_path")
    .forEach((b) => {
      const img = document.createElement("img");
      img.src = assetUrlForImagePath(b.path);
      img.alt = "";
      container.appendChild(img);
    });
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
  const chatAttachBtn = document.getElementById("chatAttachBtn");
  const chatFileInput = document.getElementById("chatFileInput");
  const chatAttachStripEl = document.getElementById("chatAttachStrip");
  const chatPaneEl = document.getElementById("chat");

  // turn number -> {row, textEl, toolsEl, metaEl, userEl, tools: Map<tool_use_id, {card, resultEl}>}
  const turnEls = new Map();
  // request_id -> element
  const pendingEls = new Map();
  // "att:<id>" for each entry in state.chat.attachments, whatever state it is
  // in. Keyed by the store's id rather than by `path`, since a chip is drawn
  // for an upload still in flight and that has no path yet.
  const attachEls = new Map();

  function buildAttachChip() {
    const chip = document.createElement("div");
    chip.className = "attachchip";
    const spinner = document.createElement("div");
    spinner.className = "attachspinner";
    const thumb = document.createElement("img");
    thumb.className = "attachthumb";
    thumb.hidden = true;
    thumb.alt = "";
    const errText = document.createElement("span");
    errText.className = "attacherr";
    errText.hidden = true;
    const removeBtn = document.createElement("button");
    removeBtn.type = "button";
    removeBtn.className = "attachremove";
    removeBtn.textContent = "×";
    removeBtn.setAttribute("aria-label", "Remove attachment");
    chip.appendChild(spinner);
    chip.appendChild(thumb);
    chip.appendChild(errText);
    chip.appendChild(removeBtn);
    return { chip, spinner, thumb, errText, removeBtn };
  }

  // `att` is one entry of state.chat.attachments; the chip element is reused
  // across its states, keyed by `attachEls` above.
  function updateAttachChip(rec, att) {
    const uploading = att.state === "uploading";
    const errored = att.state === "error";
    const done = att.state === "done";
    rec.chip.classList.toggle("error", errored);
    rec.spinner.hidden = !uploading;
    rec.thumb.hidden = !done;
    if (done) rec.thumb.src = att.url;
    rec.errText.hidden = !errored;
    if (errored) rec.errText.textContent = att.message;
    // Not removable while still in flight: dropping the slot now would leave
    // the upload with nowhere to land and no failure yet to dismiss.
    rec.removeBtn.hidden = uploading;
    // Triggers the store's own "chat" subscriber (render(), below), which
    // redraws this strip with the entry gone.
    rec.removeBtn.onclick = () => store.dropChatAttachment(att.id);
  }

  function renderAttachStrip(chat) {
    const keys = chat.attachments.map((a) => "att:" + a.id);
    chatAttachStripEl.hidden = keys.length === 0;
    chat.attachments.forEach((att) => {
      const key = "att:" + att.id;
      let rec = attachEls.get(key);
      if (!rec) {
        rec = buildAttachChip();
        attachEls.set(key, rec);
        chatAttachStripEl.appendChild(rec.chip);
      }
      updateAttachChip(rec, att);
    });
    for (const [key, rec] of attachEls) {
      if (!keys.includes(key)) {
        rec.chip.remove();
        attachEls.delete(key);
      }
    }
  }

  // The one function all three ways of attaching an image call: the file
  // picker, a paste onto the textarea, and a drop onto this pane. Each file
  // gets its own chip immediately, in the "uploading" state, so a slow
  // upload never leaves the human wondering whether the drop or paste was
  // even noticed.
  function handleFile(file) {
    const refusal = localAttachmentRefusal(file);
    if (refusal) {
      toast(refusal, false);
      return;
    }
    // Not awaited: several files dropped at once each get their chip and their
    // request straight away, and each lands in the slot it reserved here.
    uploadImage(file, "upload");
  }

  chatAttachBtn.addEventListener("click", () => chatFileInput.click());
  chatFileInput.addEventListener("change", () => {
    Array.from(chatFileInput.files || []).forEach(handleFile);
    // Cleared so picking the same file again still fires "change".
    chatFileInput.value = "";
  });

  chatInputEl.addEventListener("paste", (e) => {
    const files = Array.from((e.clipboardData && e.clipboardData.files) || []).filter((f) =>
      f.type.startsWith("image/"),
    );
    if (!files.length) return; // an ordinary text paste: leave it to the browser
    e.preventDefault();
    files.forEach(handleFile);
  });

  // dragover must be prevented for `drop` to fire at all (the platform
  // default is to refuse the pane as a drop target); drop must be prevented
  // separately, or a browser that reaches this handler navigates the whole
  // tab to the dropped file once the handler returns.
  chatPaneEl.addEventListener("dragover", (e) => {
    e.preventDefault();
    chatPaneEl.classList.add("dragover");
  });
  chatPaneEl.addEventListener("dragleave", (e) => {
    if (!e.relatedTarget || !chatPaneEl.contains(e.relatedTarget)) {
      chatPaneEl.classList.remove("dragover");
    }
  });
  chatPaneEl.addEventListener("drop", (e) => {
    e.preventDefault();
    chatPaneEl.classList.remove("dragover");
    Array.from((e.dataTransfer && e.dataTransfer.files) || []).forEach(handleFile);
  });

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
    const userAttachmentsEl = document.createElement("div");
    userAttachmentsEl.className = "attachthumbs";
    userEl.appendChild(userText);
    userEl.appendChild(userAttachmentsEl);

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

    return { row, userEl, userText, userAttachmentsEl, textEl, toolsEl, metaEl, tools: new Map() };
  }

  function updateTurnRow(rec, t) {
    if (t.user) {
      rec.userEl.hidden = false;
      rec.userText.textContent = blocksToText(t.user);
      renderUserAttachments(rec.userAttachmentsEl, t.user);
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
  }

  // The one writer of the Send button's state, because there are two
  // independent reasons to refuse a send and a function per reason would leave
  // whichever ran last deciding for both.
  //
  // An upload still in flight is a refusal rather than a partial send: posting
  // the turn without it would silently omit the image the human is waiting on
  // and then carry it on whatever they typed next, which reads as the picture
  // having been ignored.
  function renderSendButton(chat) {
    const unavailable = chat.agentStatus === "unavailable";
    const uploading = chat.attachments.some((a) => a.state === "uploading");
    chatSendBtn.disabled = unavailable || uploading;
    chatSendBtn.title = uploading
      ? "Waiting for an attachment to finish uploading"
      : (unavailable ? AGENT_TITLE.unavailable : "");
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
    renderAttachStrip(chat);
    renderSendButton(chat);
  }

  store.subscribe("chat", render);
  render();

  function doSend() {
    const text = chatInputEl.value.trim();
    const attachments = store.getState().chat.attachments;
    // Anything still uploading holds the send: the button is already disabled
    // for it, and this covers the Enter key, which is not the button.
    if (attachments.some((a) => a.state === "uploading")) return;
    const ready = attachments.filter((a) => a.state === "done");
    if (!text && !ready.length) return; // an attachment with no text is allowed; neither is not
    const blocks = ready.map((a) => ({ type: "image_path", path: a.path }));
    // Only when there is something in it. An empty text block is refused by
    // the API outright, failing the whole turn including the image, and an
    // attachment with no typed message is a case this composer allows.
    if (text) blocks.push({ type: "text", text });
    store.queueChatUserTurn(blocks);
    send({ v: 1, type: "turn", blocks });
    chatInputEl.value = "";
    store.clearChatAttachments();
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
