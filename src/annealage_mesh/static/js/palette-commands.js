/**
 * The commands behind the title bar's command field (Ctrl K), registered with
 * the style library's palette. Each one does what its button does, by clicking
 * that button or calling the same store mutator, so there is one code path.
 * A command that cannot run says why instead of vanishing.
 */

import { store } from "./store.js";

const $ = (id) => document.getElementById(id);

export function initPaletteCommands() {
  const palette = window.annealagePalette;
  if (!palette) return;
  const frame = () => window.annealageFrame.current;
  const agentUp = () => $("agentStatus").dataset.state !== "unavailable";
  const noAgent = "The agent is not available";
  const scheme = (v) => () => window.annealageScheme.set(v);

  palette.register([
    { id: "tool.fit", title: "Fit view", group: "View", shortcut: "F", run: () => $("fit").click() },
    { id: "axis.z", title: "Z-up", group: "View", run: () => $("upZ").click() },
    { id: "axis.y", title: "Y-up", group: "View", run: () => $("upY").click() },
    { id: "tool.nav", title: "Navigate", group: "Tools", run: () => $("navTool").click() },
    { id: "tool.pin", title: "Add pin", group: "Tools", shortcut: "A", run: () => $("pinTool").click() },
    { id: "tool.measure", title: "Measure", group: "Tools", hint: "Opens the Measure section", run: () => $("measureTool").click() },
    { id: "tool.sketch", title: "Sketch", group: "Tools", run: () => $("sketchBtn").click() },
    { id: "panel.review", title: "Show or hide the review panel", group: "Layout", keywords: ["panel", "inspector"], run: () => $("panelBtn").click() },
    { id: "layout.drawing", title: "Layout: Model", group: "Layout", keywords: ["focus", "drawing"], run: () => frame().setLayout("drawing") },
    { id: "layout.split", title: "Layout: Split", group: "Layout", keywords: ["focus"], run: () => frame().setLayout("split") },
    { id: "layout.agent", title: "Layout: Agent", group: "Layout", keywords: ["focus", "chat"], run: () => frame().setLayout("agent") },
    {
      id: "agent.message", title: "Message the agent", group: "Agent",
      enabled: agentUp, disabledReason: noAgent,
      run: () => {
        if (frame().isNarrow()) frame().activateTab("chat");
        $("chatInput").focus();
      },
    },
    {
      id: "review.submit", title: "Submit pins", group: "Review",
      enabled: () => store.getState().pins.length > 0, disabledReason: "No pins to submit",
      run: () => $("submit").click(),
    },
    { id: "settings", title: "Settings", group: "Mesh", run: () => $("settingsBtn").click() },
    { id: "scheme.system", title: "Colours: System", group: "Colours", run: scheme(null) },
    { id: "scheme.light", title: "Colours: Light", group: "Colours", run: scheme("light") },
    { id: "scheme.dark", title: "Colours: Dark", group: "Colours", run: scheme("dark") },
  ]);
}
