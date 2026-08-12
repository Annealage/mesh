/**
 * The single writer for viewer application state.
 *
 * Every other module reads state through `store.getState()` or a subscriber
 * callback and changes it only by calling one of the mutators exported on
 * `store` below. No line outside this file ever assigns to `state`, which is
 * what makes the checkbox-vs-mesh visibility binding in models.js
 * bidirectional: both are readers of the same `state.visibility`, and the
 * only way either one changes is through `setVisibility`, so a programmatic
 * call moves both.
 *
 * State is plain, serialisable data: numbers, strings, booleans, plain
 * arrays and plain objects only. A pin's location is a 3-element number
 * array, not a THREE.Vector3; a model's colour is a hex number, not a
 * THREE.Color. No THREE.Object3D (Mesh, Sprite, Group) is ever a state
 * value. This is what would let a later milestone deliver pins over a
 * WebSocket by calling `addPin`/`removePin` from a message handler instead
 * of from a pointer event, with no second writer to reconcile against. Any
 * THREE object that represents a piece of state, a pin's marker mesh, a
 * loaded model's Mesh, lives in a side table in whichever module created it
 * (pins.js, models.js) and is reconciled against store state by a
 * subscriber; it never becomes a state value itself.
 *
 * State shape:
 *   models        [{name, file, path, rel, label, color}, ...]
 *                 manifest entries as fetched, plus a display `color`
 *                 (a hex number) assigned by models.js from a fixed
 *                 palette. `rel` is the key used everywhere else.
 *   visibility    {rel: boolean}
 *                 per-part show/hide, keyed by the same `rel` as `models`.
 *   mode          'nav' | 'annotate'
 *   upAxis        'z' | 'y'
 *   pins          [{id, part, rel, point, normal, faceIndex, label,
 *                   comment}, ...]
 *                 user-authored pins. `part` is the model's display label
 *                 at the moment the pin was placed (kept verbatim for
 *                 /submit, which is a published contract); `label` is the
 *                 picked face's axis-aligned direction, e.g. '+X'.
 *   selectedPinId number | null
 *   callouts      [...]
 *                 the latest agent-authored callout list, exactly as
 *                 /callouts returned it, used for the sidebar list, the
 *                 measure dropdowns and the agent markers.
 *   dirty         boolean
 *                 true once a pin has been added, edited or removed since
 *                 the last successful submit.
 *   showUser      boolean, showAgent boolean
 *                 the two legend toggles.
 *   measure       {a: string, b: string}
 *                 the two measure-dropdown selections, each a measurable
 *                 key ('u<id>' for a user pin, 'a<id>' for an agent pin) or
 *                 '' for unset.
 *   activeTab     string
 *                 which narrow-viewport tab is showing; the id of one of
 *                 layout.js's TABS entries.
 *   panelOpen     boolean
 *                 whether the side panel is shown at wide viewports.
 *   connection    'connecting' | 'live' | 'polling' | 'refused'
 *                 ws.js's view of the /ws socket, read by the topbar
 *                 indicator. 'live' means callouts arrive by push;
 *                 'polling' means ws.js has fallen back to the 1.5s
 *                 /callouts poll, whether because the socket has not yet
 *                 reconnected or because it never will again this page
 *                 load (a protocol-version mismatch); 'refused' means a
 *                 pre-handshake 403 was confirmed, most likely a stale
 *                 token from a server restart, and the fallback poll runs
 *                 under this state too.
 *   chat          {turns, pendingUser, pending, agentStatus, banner}
 *                 the whole chat pane's state.
 *                 `turns` is one record per SDK turn number, `{turn, user,
 *                 text, tools, stopReason, costUsd, complete}`, built up
 *                 as text_delta/tool_use/tool_result/turn_end events
 *                 arrive; `tools` is `[{tool_use_id, name, input, result}]`
 *                 with `result` null until a tool_result names that
 *                 tool_use_id. `user` pairs the record with the composer
 *                 blocks that started it, taken off `pendingUser` the
 *                 first time any event for that turn number is seen,
 *                 because the outbound `turn` frame carries no id the
 *                 server echoes back to correlate the two. `pendingUser`
 *                 is that queue: blocks the composer has sent whose turn
 *                 number has not appeared yet.
 *                 `pending` is outstanding permission requests, one entry
 *                 per live `request_id`, `{request_id, tool, input,
 *                 suggestions}`; a replayed duplicate of a still-unanswered
 *                 request is not added twice.
 *                 `agentStatus` mirrors the hello frame's `session.agent`
 *                 ('connecting' | 'ready' | 'unavailable'). `banner` is the most
 *                 recent `session_reset` or `agent_error` event as
 *                 `{kind, text}`, or null once dismissed. A `session_reset`
 *                 also clears `turns` (`resetChatTurns`), because the new
 *                 session's turn numbers start over from 1.
 */

let state = Object.freeze({
  models: Object.freeze([]),
  visibility: Object.freeze({}),
  mode: "nav",
  upAxis: "z",
  pins: Object.freeze([]),
  selectedPinId: null,
  callouts: Object.freeze([]),
  dirty: false,
  showUser: true,
  showAgent: true,
  measure: Object.freeze({ a: "", b: "" }),
  activeTab: "model",
  panelOpen: false,
  connection: "connecting",
  chat: Object.freeze({
    turns: Object.freeze([]),
    pendingUser: Object.freeze([]),
    pending: Object.freeze([]),
    agentStatus: "connecting",
    banner: null,
  }),
});

// Assigns pin ids. Kept outside `state` because it is a generator, not a
// fact about the current app; a pin's id, once assigned, is the fact.
let nextPinId = 1;

const listeners = new Map(); // key (or '*') -> Set<fn(state)>

// True for the duration of a notify pass (the forEach in `apply`). Guards
// against a mutator being called synchronously from inside a subscriber,
// which is a real shape in this app: measure.js's 'pins' and 'callouts'
// subscriber re-validates the two dropdown selections against the pins that
// still exist, and calls setMeasure from inside that same notification when a
// selected pin has gone. Running that call inline would notify a second time
// from inside the first pass's own listener loop, so a listener later in the
// pass would observe a state change its earlier siblings never saw. Queueing
// defers it, so every listener sees one complete state change at a time, in
// the order the mutators were actually called.
let notifying = false;
const pending = [];

function commit(mutate, changedKeys) {
  if (notifying) {
    pending.push([mutate, changedKeys]);
    return;
  }
  apply(mutate, changedKeys);
  // Drained here, by the outermost commit, as a flat loop. A queued mutator
  // that queues another one appends to this same array rather than nesting a
  // commit inside a commit, so a long chain of re-entrant changes iterates
  // instead of growing the stack.
  while (pending.length) {
    const next = pending.shift();
    apply(next[0], next[1]);
  }
}

function apply(mutate, changedKeys) {
  mutate();
  state = Object.freeze(state);
  notifying = true;
  try {
    const keys = new Set([...changedKeys, "*"]);
    keys.forEach((k) => {
      const set = listeners.get(k);
      if (set) set.forEach((fn) => fn(state));
    });
  } finally {
    notifying = false;
  }
}

function getState() {
  return state;
}

function subscribe(key, fn) {
  if (!listeners.has(key)) listeners.set(key, new Set());
  listeners.get(key).add(fn);
  return () => listeners.get(key).delete(fn);
}

function setModels(models) {
  commit(() => {
    const frozenModels = Object.freeze(models.map((m) => Object.freeze({ ...m })));
    // Default visibility (first model shown, rest hidden) applies only to a
    // rel not already in the map, so a rescan in a later milestone would
    // not undo a toggle the reviewer already made.
    const visibility = { ...state.visibility };
    frozenModels.forEach((m, i) => {
      if (!(m.rel in visibility)) visibility[m.rel] = i === 0;
    });
    state = { ...state, models: frozenModels, visibility: Object.freeze(visibility) };
  }, ["models", "visibility"]);
}

function setVisibility(rel, on) {
  commit(() => {
    state = { ...state, visibility: Object.freeze({ ...state.visibility, [rel]: !!on }) };
  }, ["visibility"]);
}

function setMode(mode) {
  commit(() => {
    state = { ...state, mode };
  }, ["mode"]);
}

function setUpAxis(axis) {
  commit(() => {
    state = { ...state, upAxis: axis };
  }, ["upAxis"]);
}

function addPin(data) {
  const id = nextPinId++;
  commit(() => {
    const pin = Object.freeze({ id, comment: "", ...data });
    state = {
      ...state,
      pins: Object.freeze([...state.pins, pin]),
      selectedPinId: id,
      dirty: true,
    };
  }, ["pins", "selectedPinId", "dirty"]);
  return id;
}

function removePin(id) {
  commit(() => {
    const pins = state.pins.filter((p) => p.id !== id);
    const selectedPinId = state.selectedPinId === id ? null : state.selectedPinId;
    state = { ...state, pins: Object.freeze(pins), selectedPinId, dirty: true };
  }, ["pins", "selectedPinId", "dirty"]);
}

function setPinComment(id, comment) {
  commit(() => {
    const pins = state.pins.map((p) => (p.id === id ? Object.freeze({ ...p, comment }) : p));
    state = { ...state, pins: Object.freeze(pins), dirty: true };
  }, ["pins", "dirty"]);
}

function selectPin(id) {
  commit(() => {
    state = { ...state, selectedPinId: id };
  }, ["selectedPinId"]);
}

function clearPins() {
  commit(() => {
    state = { ...state, pins: Object.freeze([]), selectedPinId: null, dirty: true };
  }, ["pins", "selectedPinId", "dirty"]);
}

function setCallouts(list) {
  commit(() => {
    state = { ...state, callouts: Object.freeze(list.map((c) => Object.freeze({ ...c }))) };
  }, ["callouts"]);
}

function setDirty(v) {
  commit(() => {
    state = { ...state, dirty: !!v };
  }, ["dirty"]);
}

function setShowUser(v) {
  commit(() => {
    state = { ...state, showUser: !!v };
  }, ["showUser"]);
}

function setShowAgent(v) {
  commit(() => {
    state = { ...state, showAgent: !!v };
  }, ["showAgent"]);
}

function setMeasure(aKey, bKey) {
  commit(() => {
    state = { ...state, measure: Object.freeze({ a: aKey, b: bKey }) };
  }, ["measure"]);
}

function setActiveTab(tabId) {
  commit(() => {
    state = { ...state, activeTab: tabId };
  }, ["activeTab"]);
}

function setPanelOpen(v) {
  commit(() => {
    state = { ...state, panelOpen: !!v };
  }, ["panelOpen"]);
}

function setConnection(v) {
  commit(() => {
    state = { ...state, connection: v };
  }, ["connection"]);
}

// Finds the turn record for `turn` in `chat.turns`, or builds one, pairing it
// with the oldest still-unmatched entry in `chat.pendingUser` (the outbound
// `turn` frame carries no id the server echoes back, so the first event for
// a turn number is what associates it with the composer blocks that started
// it). Callers must fold the returned `turns` and `pendingUser` back into a
// new chat object themselves; this only computes the two arrays, it does not
// touch `state`.
function ensureTurn(chat, turn) {
  const idx = chat.turns.findIndex((t) => t.turn === turn);
  if (idx !== -1) {
    return { turns: chat.turns, pendingUser: chat.pendingUser, idx };
  }
  const user = chat.pendingUser.length ? chat.pendingUser[0] : null;
  const pendingUser = chat.pendingUser.length
    ? Object.freeze(chat.pendingUser.slice(1))
    : chat.pendingUser;
  const record = Object.freeze({
    turn,
    user,
    text: "",
    tools: Object.freeze([]),
    stopReason: null,
    costUsd: null,
    complete: false,
  });
  const turns = Object.freeze([...chat.turns, record]);
  return { turns, pendingUser, idx: turns.length - 1 };
}

function appendChatTextDelta(turn, text) {
  commit(() => {
    const chat = state.chat;
    const { turns, pendingUser, idx } = ensureTurn(chat, turn);
    const nextTurns = Object.freeze(
      turns.map((t, i) => (i === idx ? Object.freeze({ ...t, text: t.text + text }) : t)),
    );
    state = { ...state, chat: Object.freeze({ ...chat, turns: nextTurns, pendingUser }) };
  }, ["chat"]);
}

function addChatToolUse(turn, toolUseId, name, input) {
  commit(() => {
    const chat = state.chat;
    const { turns, pendingUser, idx } = ensureTurn(chat, turn);
    const tool = Object.freeze({ tool_use_id: toolUseId, name, input, result: null });
    const nextTurns = Object.freeze(
      turns.map((t, i) =>
        i === idx ? Object.freeze({ ...t, tools: Object.freeze([...t.tools, tool]) }) : t,
      ),
    );
    state = { ...state, chat: Object.freeze({ ...chat, turns: nextTurns, pendingUser }) };
  }, ["chat"]);
}

// Attaches a tool's result to the tool_use record it belongs to, wherever in
// `turns` that record is; a tool_result's frame carries only the
// tool_use_id, not the turn number, so every turn is searched rather than
// assuming the result lands in the same turn its tool_use did.
function setChatToolResult(toolUseId, isError, text) {
  commit(() => {
    const chat = state.chat;
    const turns = Object.freeze(
      chat.turns.map((t) => {
        if (!t.tools.some((tool) => tool.tool_use_id === toolUseId)) return t;
        const tools = Object.freeze(
          t.tools.map((tool) =>
            tool.tool_use_id === toolUseId
              ? Object.freeze({ ...tool, result: Object.freeze({ isError: !!isError, text }) })
              : tool,
          ),
        );
        return Object.freeze({ ...t, tools });
      }),
    );
    state = { ...state, chat: Object.freeze({ ...chat, turns }) };
  }, ["chat"]);
}

function endChatTurn(turn, stopReason, costUsd) {
  commit(() => {
    const chat = state.chat;
    const { turns, pendingUser, idx } = ensureTurn(chat, turn);
    const nextTurns = Object.freeze(
      turns.map((t, i) =>
        i === idx ? Object.freeze({ ...t, stopReason, costUsd, complete: true }) : t,
      ),
    );
    state = { ...state, chat: Object.freeze({ ...chat, turns: nextTurns, pendingUser }) };
  }, ["chat"]);
}

function queueChatUserTurn(blocks) {
  commit(() => {
    const chat = state.chat;
    state = {
      ...state,
      chat: Object.freeze({ ...chat, pendingUser: Object.freeze([...chat.pendingUser, blocks]) }),
    };
  }, ["chat"]);
}

// Adds a permission_request to the pending list unless its request_id is
// already there. Replay re-emits every still-unanswered request on
// reconnect, so this call is not proof of a first sighting.
function addChatPermissionRequest(requestId, tool, input, suggestions) {
  commit(() => {
    const chat = state.chat;
    if (chat.pending.some((p) => p.request_id === requestId)) return;
    const entry = Object.freeze({
      request_id: requestId,
      tool,
      input,
      suggestions: suggestions || null,
    });
    state = {
      ...state,
      chat: Object.freeze({ ...chat, pending: Object.freeze([...chat.pending, entry]) }),
    };
  }, ["chat"]);
}

// Records that this view has sent a decision for `requestId` and is waiting to
// hear that it landed. The card stays on screen, showing that it is in flight,
// because it is the server's `permission_resolved` that says a request is over,
// and only that event distinguishes a decision that took effect from one
// another view had already answered.
function markChatPermissionSubmitted(requestId, decision) {
  commit(() => {
    const chat = state.chat;
    let changed = false;
    const pending = Object.freeze(
      chat.pending.map((p) => {
        if (p.request_id !== requestId || p.submitted) return p;
        changed = true;
        return Object.freeze({ ...p, submitted: decision });
      }),
    );
    if (!changed) return;
    state = { ...state, chat: Object.freeze({ ...chat, pending }) };
  }, ["chat"]);
}

function removeChatPermissionRequest(requestId) {
  commit(() => {
    const chat = state.chat;
    const pending = Object.freeze(chat.pending.filter((p) => p.request_id !== requestId));
    state = { ...state, chat: Object.freeze({ ...chat, pending }) };
  }, ["chat"]);
}


function setChatAgentStatus(status) {
  commit(() => {
    state = { ...state, chat: Object.freeze({ ...state.chat, agentStatus: status }) };
  }, ["chat"]);
}

function setChatBanner(kind, text) {
  commit(() => {
    state = { ...state, chat: Object.freeze({ ...state.chat, banner: Object.freeze({ kind, text }) }) };
  }, ["chat"]);
}

function clearChatBanner() {
  commit(() => {
    state = { ...state, chat: Object.freeze({ ...state.chat, banner: null }) };
  }, ["chat"]);
}

// Empties `turns` without touching `pendingUser`. Called when a
// session_reset event reports the SDK started a fresh session: that
// session's own turn numbering starts from 1 again, so leaving the old
// turns in place risks a later ensureTurn call finding a stale record
// under the same turn number and folding new content into it. A message
// already queued in `pendingUser` still belongs to the next turn the new
// session produces, so it is left alone.
function resetChatTurns() {
  commit(() => {
    state = { ...state, chat: Object.freeze({ ...state.chat, turns: Object.freeze([]) }) };
  }, ["chat"]);
}

export const store = {
  getState,
  subscribe,
  setModels,
  setVisibility,
  setMode,
  setUpAxis,
  addPin,
  removePin,
  setPinComment,
  selectPin,
  clearPins,
  setCallouts,
  setDirty,
  setShowUser,
  setShowAgent,
  setMeasure,
  setActiveTab,
  setPanelOpen,
  setConnection,
  appendChatTextDelta,
  addChatToolUse,
  setChatToolResult,
  endChatTurn,
  queueChatUserTurn,
  addChatPermissionRequest,
  markChatPermissionSubmitted,
  removeChatPermissionRequest,
  setChatAgentStatus,
  setChatBanner,
  clearChatBanner,
  resetChatTurns,
};
