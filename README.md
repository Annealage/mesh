# Annealage Mesh

Annealage Mesh is a little web tool for building 3D-printable parts with an agent, by pointing at them. You give it a folder, it serves up a 3D viewer in the browser with a Claude Code chat pane beside it, and the two of you get to work in there.

Ask for a part and the agent writes the CAD script in that folder, runs it, and the STL turns up in the viewer. Click the face that's wrong, say what's wrong with it, and off it goes to fix the script.

![Annealage Mesh: the model with a human's orange pin and the agent's cyan callouts, the review panel, and the chat pane mid-answer](docs/mesh-three-pane.png)

## Why

I built this while iterating on a 3D-printed part with Claude Code. The CAD was generated from a script, I'd look at a render, and then spend ages typing things like "no, the inside corner on the far wall near the fan, not that one" trying to describe which face I meant. It was a pain, and half the time the agent picked the wrong spot anyway.

Pointing at the thing is just so much easier. So we built a viewer where I click the face and type the comment right there, and the agent gets it back with the actual coordinates, no guessing.

It's bidirectional too, which turned out to be the good bit. The agent can write its own callouts (a location plus a note) and they show up as pins in the viewer for me to see and reply to. So it ends up being a shared surface, I mark up what I want changed, the agent pins its questions on the geometry, and we go back and forth pointing at the same model instead of describing it in words.

## Install

Python 3.10+, with two runtime dependencies: [microdot](https://github.com/miguelgrinberg/microdot), which is pure Python, and the Claude Agent SDK, which is how the chat pane talks to Claude Code. three.js 0.160.0 is vendored inside the package and served locally, so the viewer itself needs no network access at all.

On Linux you'll also want `bubblewrap` and `socat`:

    apt install bubblewrap socat

That's what keeps the agent's shell contained, and agent mode won't start without them rather than quietly running you an uncontained one. macOS has its own sandbox built into the OS so there's nothing to install there. If you'd rather not bother, `--no-agent` gives you the viewer on its own and needs neither.

Run it straight from GitHub, nothing to install, via uv:

    uvx --from git+https://github.com/Annealage/mesh annealage-mesh ./path/to/part

Or install it as a tool:

    uv tool install git+https://github.com/Annealage/mesh
    # or
    pipx install git+https://github.com/Annealage/mesh

Once it's up on PyPI that shortens to `uvx annealage-mesh ./part`.

## Usage

Point it at a folder:

    annealage-mesh ./build

It starts a local server, prints the URL with a per-run token in it, and opens your browser. Every `.stl` in there shows up in the viewer, toggle them on/off in the side panel.

- Drag to orbit, scroll / pinch to zoom, right-drag or two-finger to pan.
- Flip to "Add pin" mode, click the model to drop a pin, then type a comment against it in the panel.
- Hit Submit. Your pins get written to `mesh-comments.json` in the served folder, which is what the agent reads.
- Type in the chat pane to put the agent to work in that folder. Interrupt stops a turn mid-flight, and each turn shows what it cost.
- The agent works the viewer too, not just the folder. It can move the camera, hide and show parts, screenshot what's on screen and pin its own callouts, so "show me the underside of that boss" is something it does rather than tells you to do.
- Hit Pause in the topbar and everything that changes the view gets refused until you hit it again, so you can line up a shot or type a comment without it moving underneath you. It can still look while paused.
- Need a distance between two features? Pick any two placed pins (yours or the agent's) in the "Measure" panel for ΔX/ΔY/ΔZ and the direct distance, drawn as a line in the view.

It works on a phone too, the three panes become tabs and navigation is all touch (one finger orbits, two fingers pan / zoom). `--host tailscale` binds your tailnet address instead of loopback, which is what I use to look at a part on my phone while the agent iterates on the desktop.

Sessions are kept, so `-c` picks up the most recent conversation for that folder and `-r` lists what's there. Reloading the browser mid-turn doesn't lose anything, the conversation belongs to the session rather than the socket.

There's a fuller walkthrough in [docs/user-guide.md](docs/user-guide.md) covering the loop, the flags, remote access and what to do when something's off.

A few files turn up in the served folder:

- `mesh-comments.json` - your pins and comments, written on submit (also appended to `mesh-comments.log`).
- `mesh-callouts.json` - callouts to show in the viewer. Write pins here and they appear live (cyan, read-only). This is how an agent points back at the model.
- `images/` - screenshots the agent saved, meant to be committed.
- `.mesh/` - session transcripts, any allow-always decisions you made, and a lock file so two servers can't fight over one folder.

## What it'll ask you about

The agent's shell runs sandboxed, so a command that stays inside the project folder just runs without asking. That's deliberate, regenerating a part twenty times would be miserable otherwise. Anything that writes through its edit tools, wants out of the folder, or reaches the network gets you a card in the chat pane with the full command or file contents on it, and you allow it, allow it for the rest of the session, or deny it with a reason. The reason goes to the agent verbatim, so "not that file, do the enclosure instead" is more use to it than a bare no.

Its viewer tools split the same way, by whether they change anything. Reading the camera, the part list, your comments or a screenshot of the view never asks. Moving the camera, hiding a part, writing a callout or saving a screenshot to disk gets a card the first time, and "Always allow" on one of those is remembered for the folder.

![An approval card for a Write, showing the whole file path and contents, with Allow, Always allow and Deny](docs/mesh-approval.png)

Two things worth knowing about the containment. It stops writes and network, not reads, so a sandboxed shell can still read anything your user can. And the model can't drop the sandbox for a command by asking, which it does try if you let it.

If the folder you point it at has its own `.claude/settings.json` or `.mcp.json` in it, mesh won't start the agent until you've said you trust that folder. Those files can declare hooks, hooks are shell commands, and one kind runs before you've typed anything at all, so an unpacked model archive off the internet isn't something to hand a shell to sight unseen. Read them, then `--trust-project-config` accepts them, recorded against the exact contents you read so any later edit asks again.

It binds to `127.0.0.1` by default, and the startup banner tells you what it's reachable on every run. On anything that isn't loopback the token in the URL stops being defence in depth and becomes the only thing between the network and an agent with a shell, so keep that URL to yourself.

## For AI agents

If you're an agent (or setting one up) working outside the chat pane, the contract is still just two JSON files in the served folder, unchanged.

Read the human's feedback from `mesh-comments.json`:

    {
      "submitted_at": "...",
      "count": 1,
      "annotations": [
        { "id": 1, "part": "bracket", "label": "+Z", "point": [12.5, -3.2, 44.0],
          "normal": [0, 0, 1], "faceIndex": 1234, "comment": "this fillet's too sharp" }
      ]
    }

`point` is the click location in model space (same units as the STL), so you can map a comment straight to a spot in the CAD script that generated it.

Write your own callouts to `mesh-callouts.json` and they show up as cyan pins in the viewer, live:

    {
      "annotations": [
        { "id": 1, "author": "agent", "part": "bracket", "label": "+Y", "point": [0, 20, 10],
          "comment": "moved this wall out 2mm, that clear enough?" }
      ]
    }

`point` and `comment` are the only fields that really matter, the rest are display niceties.

There's also a Claude Code skill in `skill/` that wires this up as a workflow, so you can just tell Claude to use Annealage Mesh when it's working on printable models.

## Licence

[PolyForm Noncommercial 1.0.0](LICENSE), free to use for any noncommercial purpose. Commercial use needs a separate licence; see [COMMERCIAL.md](COMMERCIAL.md).

The Claude Code skill in [`skill/annealage-mesh/`](skill/annealage-mesh/) is MIT, so it can be copied into any agent configuration without restriction.
