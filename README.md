# Annealage Mesh

Reviewing a 3D-printed part with an AI agent means describing geometry in words. "No, the inside corner on the far wall near the fan, not that one." It is slow, and half the time the agent picks the wrong face anyway.

Annealage Mesh gives you both a model and an agent in one window. Point it at a folder of STL files and it opens a 3D viewer with a chat pane beside it. You click the model to drop a comment pinned to those exact coordinates; the agent reads the coordinates, works in that folder, and pins its own callouts back onto the geometry where you can see them. Nobody has to describe anything.

![The three-pane view: the model with a human's orange pin and the agent's cyan callouts, the review panel, and the chat pane mid-answer](docs/mesh-three-pane.png)

## What it does

**Point instead of describing.** Click a face, type a comment. The pin carries the click location in model space, the surface normal and the part name, so a comment maps to a spot in the CAD script that generated it.

**The agent is in the window.** The chat pane is a full Claude Code session whose working directory is the folder being served, so it can read your CAD script, run the generator, measure the STL and edit the source, and you watch it happen next to the model it is talking about. It streams token by token and shows the cost of each turn.

**Callouts come back onto the geometry.** The agent writes a location plus a note and it appears as a cyan pin in your view, live. Review becomes a shared surface pointing at one model instead of two people describing it.

**You approve what matters.** A shell command confined to the project folder runs without asking. Editing a file, writing outside the folder, or reaching the network raises an approval card showing exactly what was requested, and you allow it, always-allow it, or deny it with a reason the agent receives.

![An approval card for a Write, showing the full file path and content, with Allow, Always allow and Deny](docs/mesh-approval.png)

**It works from a phone.** The panes become tabs, navigation is touch, and pins and approvals both work. Reviewing a print on the phone next to the printer while the agent iterates on the desktop is a real workflow, and `--host tailscale` is one flag.

**Measure between any two pins.** Pick two pins, yours or the agent's, for ΔX/ΔY/ΔZ and the direct distance, drawn as a line in the view.

## Install

Python 3.10 or newer, and two runtime dependencies: [microdot](https://github.com/miguelgrinberg/microdot), and the Claude Agent SDK, which is how the chat pane talks to Claude Code. three.js is vendored in the package and served locally, so the viewer needs no network access.

Be ready for the size: the SDK bundles the Claude Code CLI itself, so installing pulls roughly 90 MB rather than a few tens of kilobytes. That is the cost of the agent being part of the tool rather than something you wire up separately.

Run it without installing anything:

    uvx --from git+https://github.com/Annealage/mesh annealage-mesh ./path/to/stls

Or install it as a tool:

    uv tool install git+https://github.com/Annealage/mesh
    # or
    pipx install git+https://github.com/Annealage/mesh

**On Linux, agent mode also needs `bubblewrap` and `socat`** (`apt install bubblewrap socat`, or your distribution's equivalent). They are what confines the agent's shell, and agent mode refuses to start without them rather than quietly running an uncontained one. macOS sandboxes with a mechanism built into the OS and needs nothing extra. If you would rather not install them, `--no-agent` runs the viewer alone, which needs neither.

## Using it

    annealage-mesh ./build

It serves the directory, prints the URL with a per-run token, and opens your browser. Every `.stl` in the tree appears in the viewer.

- Drag to orbit, scroll or pinch to zoom, right-drag or two fingers to pan.
- Switch to **Add pin**, click the model, then type a comment against the pin in the panel.
- **Submit** writes your pins to `mesh-comments.json` in the served directory.
- Type in the chat pane to put the agent to work in that folder.

Full walkthrough, including sessions, the approval model, remote access and every flag: **[docs/user-guide.md](docs/user-guide.md)**.

## What it puts in your folder

- `mesh-comments.json` — your pins, written on Submit, and appended to `mesh-comments.log`.
- `mesh-callouts.json` — the agent's callouts. Anything here shows up as a cyan pin, live.
- `.mesh/` — session transcripts, remembered approvals, and the lock that stops two servers fighting over one directory.

## Security

Mesh hands an AI agent a shell in a directory you chose, and serves a page that can drive it, so it is worth being plain about the boundaries.

**The agent's shell is confined.** Writes land inside the project folder; writes elsewhere and network access are refused by the sandbox or reach you as an approval card. The startup banner states which posture is actually in effect every run, rather than assuming the one it asked for. Note the honest limit: the sandbox restricts writes and network, **not reads**, so a confined shell can still read any file your user can.

**The server is bound to loopback and tokened.** Non-loopback binds are supported, not hidden behind a scare-flag, and the banner tells you what the server is reachable on every run. On any non-loopback bind the token stops being defence in depth and becomes the control that matters.

**A folder's Claude configuration is not trusted until you say so.** A `.claude/settings.json` or `.mcp.json` can name shell commands to run, one of them before any prompt is sent, and unpacking someone else's model archive is an ordinary thing to do. Agent mode refuses to start when a served folder carries configuration you have not accepted, and rechecks it on every tool call, so a change mid-session stops the agent rather than silently widening what it may do.

## Working with an agent outside the window

The chat pane is the intended way in, but the file contract is stable and unchanged, so a separately running agent still works: read `mesh-comments.json`, write `mesh-callouts.json`.

    {
      "annotations": [
        { "id": 1, "part": "bracket", "label": "+Z", "point": [12.5, -3.2, 44.0],
          "normal": [0, 0, 1], "faceIndex": 1234, "comment": "this fillet's too sharp" }
      ]
    }

`point` and `comment` are the fields that matter; the rest are display niceties. There is also a Claude Code skill in [`skill/annealage-mesh/`](skill/annealage-mesh/) that wires this up as a workflow.

## Licence

[PolyForm Noncommercial 1.0.0](LICENSE) — free for any noncommercial purpose. Commercial use needs a separate licence; see [COMMERCIAL.md](COMMERCIAL.md).

The Claude Code skill in [`skill/annealage-mesh/`](skill/annealage-mesh/) is MIT, so it can be copied into any agent configuration without restriction.
