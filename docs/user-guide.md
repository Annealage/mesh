# Annealage Mesh user guide

This is the full walkthrough for using Mesh to build and review a 3D-printable part with an agent. For what the tool is and how to install it, start with the [README](../README.md).

## Contents

- [First run](#first-run)
- [The three panes](#the-three-panes)
- [The modelling loop](#the-modelling-loop)
- [Placing pins](#placing-pins)
- [Working with the agent](#working-with-the-agent)
- [Attaching pictures, and sketching on the view](#attaching-pictures-and-sketching-on-the-view)
- [Settings, and where a value came from](#settings-and-where-a-value-came-from)
- [Exporting a transcript](#exporting-a-transcript)
- [Setting a folder up, and checking your machine](#setting-a-folder-up-and-checking-your-machine)
- [What the agent can do to the viewer](#what-the-agent-can-do-to-the-viewer)
- [Pausing the agent's view control](#pausing-the-agents-view-control)
- [Approving what the agent does](#approving-what-the-agent-does)
- [Sessions](#sessions)
- [Reviewing from a phone](#reviewing-from-a-phone)
- [Reaching it through tailscale serve](#reaching-it-through-tailscale-serve)
- [Trusting a folder's Claude configuration](#trusting-a-folders-claude-configuration)
- [Files in your directory](#files-in-your-directory)
- [Every flag](#every-flag)
- [When something is wrong](#when-something-is-wrong)

## First run

Point it at a directory of STL files:

    annealage-mesh ./build

The startup output is worth reading once, because it tells you four things you would otherwise have to guess at:

    Annealage Mesh
      serving STLs from : /home/you/build
      human comments    : /home/you/build/mesh-comments.json  (written on submit)
      comments log      : /home/you/build/mesh-comments.log
      agent callouts    : /home/you/build/mesh-callouts.json  (agent writes here to show pins)
      session           : 20260812T135919Z-96c60120  (fresh)
    Annealage Mesh is serving on loopback on 127.0.0.1:8765
      open: http://127.0.0.1:8765/#t=VMMChzhTMI_b9w8ON_IJWg
      reachable from this machine only
      agent posture: sandbox ACTIVE - bash runs contained and unprompted; edit, write and network still need your approval
      (Ctrl-C to stop)

The URL carries a per-run token in its fragment, which the browser keeps out of server logs and out of the `Referer` header. The **exposure line** says what the server is reachable on, and prints every run whether or not you passed `--host`, because a default is exactly the case where nobody typed a flag to remind them. The **agent posture** line says which containment is actually in effect rather than the one that was requested; if a dependency is missing it says so and names it.

Your browser opens automatically. `--no-open` stops that.

## The three panes

**The model**, on the left, is the 3D view. Drag to orbit, scroll or pinch to zoom, right-drag or two fingers to pan. `Fit` reframes everything visible; `Z-up` switches which axis is up, for models exported from a Y-up tool; `Pause` stops the agent changing anything in the view, and is covered under [Pausing the agent's view control](#pausing-the-agents-view-control).

**Review**, in the middle, lists the parts, your pins, the agent's callouts, and the measure controls. Every `.stl` under the served directory appears under **Parts** with a colour and a visibility checkbox, including ones generated after the page was opened. A part keeps its colour for the life of the page, so a newly generated part does not recolour the others. Hiding a part also stops you pinning it, since you can only pin what you can see.

**Chat**, on the right, is the agent. It shows a status pill (Connecting, Ready, Unavailable), the transcript, tool cards you can expand, approval cards, and the composer.

Agent health never affects the viewer. If the agent cannot start, or dies mid-session, the model, the pins and Submit keep working and the chat pane tells you what happened and what to do.

## The modelling loop

This is what the tool is for, so it is worth spelling out as a sequence.

1. **You describe the part**, in the chat pane, in whatever terms you would use to a colleague. "A shroud for a 40 mm fan, 38 mm tall, with a 3 mm flange and four M3 holes on a 32 mm square."
2. **The agent writes the source and runs it.** It has a shell in this folder, so it creates whatever script the toolchain you are using needs, runs it, and produces an STL. A command confined to this folder runs without asking, which is what keeps this step quick enough to repeat.
3. **The part appears.** A new STL shows up in the viewer with its own colour and its own checkbox; a regenerated one replaces the geometry in place, with the camera left where you put it.
4. **You point at what is wrong.** Switch to Add pin, click the face, type the problem. Submit.
5. **The agent reads the coordinates and revises.** It edits the source, regenerates, and step 3 happens again.

Repeat until the part is right, then print it.

Two details make step 3 behave sensibly rather than fighting you. A part still being written to disk is waited for rather than shown, so you never see a half-generated mesh or a load failure for a file that was about to be fine. And a part regenerated twenty times leaves one mesh in the scene, not twenty stacked copies.

Mesh does not choose your CAD toolchain, and does not supply one. Whatever the agent can install or run in that folder is what you get; OpenSCAD, build123d, CadQuery and a hand-written mesh generator all work the same way from Mesh's point of view, because all it watches for is the STL. If you have a preference, say so in the first message, or put it in a `CLAUDE.md` in the folder, which the agent reads.

## Placing pins

1. Click **Navigate** in the top bar to switch it to **Add pin**.
2. Click the model. A numbered orange pin drops exactly where you clicked.
3. Type your comment against that pin in the Review panel.
4. Click **Submit**.

Submit writes every pin to `mesh-comments.json` in the served directory and appends the same payload to `mesh-comments.log`, so the history survives being overwritten. Each pin carries the click point in model space, the surface normal, the face index and the part name.

**Measure** takes any two placed pins, yours or the agent's, and reports ΔX, ΔY, ΔZ and the direct distance, drawn as a line in the view. This is the quickest way to answer "how far is this boss from that wall" without going back to the CAD.

Pins are yours and editable; callouts from the agent are read-only, and the two toggles at the top of the Review panel show or hide each set.

## Working with the agent

The chat pane is a Claude Code session whose working directory **is** the folder being served. So it can read the CAD script that generated the STLs, run the generator, measure the mesh, and edit the source, all in the place the models came from.

Useful things to ask, in rough order of how much they play to the tool's strengths:

- "Write the OpenSCAD for a 40 mm fan shroud, 38 mm tall, and generate the STL."
- "Read `mesh-comments.json` and address the pin on the fan shroud."
- "The rim near pin 2 looks thin. Measure the wall there and tell me what it is."
- "Regenerate the shroud with a 1.2 mm rim and tell me what changed."
- "Put a callout on each face you think will need support."

That last one is the other half of the pointing: when the agent writes a callout, it appears in your view within a fraction of a second, pinned to the coordinates it chose. You can then pin a reply next to its callout and Submit, and it reads your coordinates back. Neither side ever describes a location in words.

The composer sends on the button; **Interrupt** stops a turn already in flight. Each completed turn shows its stop reason and cost.

## Attaching pictures, and sketching on the view

Sometimes the thing you want to point at is easier shown than described, and it is not always on the model: a photo of the printed part warping, a screenshot of a slicer warning, a reference drawing.

Three ways to attach an image, all doing the same thing:

- The paperclip in the composer opens a file picker.
- Paste an image into the message box. A screenshot straight off the clipboard works.
- Drag an image file anywhere onto the chat pane and drop it.

PNG, JPEG and WEBP, up to 8 MB. Each one uploads as soon as you attach it and appears as a thumbnail chip above the message box, and the × on a chip drops it. Up to four per message. Send stays disabled until every attachment has finished uploading, so a picture always arrives with the question you asked about it rather than trailing onto the next one.

Attached files are written into `images/` in the served folder under a name Mesh chooses, and they stay there after the message is sent. They are meant to be committed, the same as the agent's own screenshots.

**Sketch** is the same idea aimed at the model itself. Hit it and the 3D view freezes under a drawing layer: circle the wall that is too thin, cross out the boss you want gone, arrow at the face that should be flat. Undo removes the last stroke, Clear starts over, Escape leaves without attaching. **Attach** flattens your strokes onto the view exactly as you see it and attaches that as a picture, so the agent sees the part and your marks in one image.

Two things worth knowing about sketching. The camera is held still while you draw, because a stroke only means anything against the view it was drawn on. For the same reason, if the view does move between your first stroke and Attach, either because you resized the window or because the agent moved the camera, Attach refuses and says so rather than sending marks that point at the wrong geometry; your strokes are kept, so you can put the view back and attach then. Hit `Pause` first if you want to be certain the agent leaves the view alone while you work.

A sketch is a picture, not coordinates. If you need the agent to have exact model coordinates, place a pin.

## Settings, and where a value came from

The gear in the topbar opens Settings. Four sections: Server (host, port, whether a browser opens), Agent (model, effort, permission mode), Viewer (which axis is up, whether tool cards start closed) and a read-only Diagnostics block.

Every field says where its current value came from, because there are four places it could be and knowing which one is the difference between fixing it in a second and hunting for it. Highest wins:

1. A command-line flag, which applies to this run only.
2. This project's `.mesh/config.toml`, which is committed and shared with whoever else works in the folder.
3. Your own `settings.toml`, which lives outside the project (on Linux, `~/.config/annealage-mesh/settings.toml`) and applies to every project you open.
4. The built-in default.

Editing a field writes it to the layer that key belongs to, which is not a choice the window offers because the key already decides it. The three agent fields, model, effort and permission mode, go to the project's `.mesh/config.toml`: they are properties of the work rather than of you, and a part that needs a particular model needs it for whoever opens the folder next. Host, port, whether a browser opens and the two viewer preferences go to your own `settings.toml`, since they are properties of your machine and your habits. Anything that cannot change without a restart, which is host, port, the browser-opening and all three agent fields, says "takes effect next run" instead of pretending otherwise. There is deliberately no live rebind; moving a listening socket out from under open connections is not worth the complexity when restarting costs a second.

If you save a new port, the field afterwards shows the port you saved and the note underneath tells you which one the running server is still on. Two facts, both true, neither hidden.

Two things are never written to a config file, whatever you do: the per-run token, which is regenerated at every start, and `permission_mode: bypassPermissions`. The second is a deliberate refusal rather than an oversight. Persisting "never ask me again" for an agent that holds a shell turns one careless moment into a standing hole, so it is not offered in the window and is rejected with an error if it is found in either file.

`annealage-mesh --settings` prints the same information in the terminal and exits, naming the file behind each value.

## Exporting a transcript

Nothing conversational is written to a shareable file unless you ask. The event log under `.mesh/sessions/<id>/` is what scrollback, reconnects and `-c` all read, and it stays gitignored because it churns with every token.

"Export" in the chat header writes the conversation to `review/transcript-<timestamp>.md` and tells you the path. `review/` is created the first time you export and not before. Two exports in the same second do not overwrite each other.

The agent has the same ability as a tool, and unlike its viewer tools this one asks first, because an exported transcript can carry whatever was said about the hardware, absolute paths and what each turn cost. Your own button asks nobody: you pressed it.

## Setting a folder up, and checking your machine

`annealage-mesh <dir>` sets a folder up before it starts serving, and says what it created. What it makes: `models/` and `images/`, a `.gitignore` that ignores `.mesh/` except the shareable `config.toml`, a `CLAUDE.md` stub describing the folder's contract for an agent working in it, and a git repository with one commit if git is installed and the folder is not already in one. It is idempotent, so the second run creates nothing, and it never overwrites a `CLAUDE.md` you wrote yourself.

Nothing is ever committed after that first commit. A tool that quietly commits your working folder takes away your ability to stage your own work.

    annealage-mesh init ./part          # set it up and stop, no server
    annealage-mesh init ./part --no-git  # skip git entirely
    annealage-mesh init ./part --force   # regenerate .gitignore and CLAUDE.md

If git is installed but has no `user.email`, the repository is created and the commit is skipped, and it tells you that rather than inventing an identity.

`annealage-mesh doctor ./part` prints what this machine has and what this project looks like: Python, the `claude` CLI's path and version and whether it came bundled with the SDK or off your `PATH`, git, whether the sandbox dependencies are present, which settings files exist, and whether a lock is held on the folder. It starts no server and takes no lock, so it is safe to run against a directory that already has one running. It is the same set of facts the Diagnostics block shows, from the same code.

## What the agent can do to the viewer

The agent has tools for the viewer itself, not just for the folder, so "show me the underside of that boss" is something it can carry out rather than instruct you to do. Sixteen of them, in three grades by what a mistake would cost.

**Looking**, which changes nothing and never asks you: read the camera, list the parts and which are visible, read your submitted comments and its own callouts, read a model's triangle count and bounding box, screenshot the view as it stands, and measure between any two placed pins.

**Driving**, which changes what is on your screen and nothing else, and also never asks: move the camera or reframe it, show or hide a part, switch the up axis, select one of your pins. This is deliberate rather than lax. You are looking at the screen while it happens, and the loop this tool is for has the agent reframing a part it has just regenerated several times a turn, so a card for each of those would either be clicked without reading or turned off with one standing grant. **Pause** is the control for these, and it is covered below.

**Writing**, which leaves something behind after you close the page, and always asks: add or delete a callout, and save a screenshot into `images/`.

The screenshot one is worth knowing about, because it changes what the agent can answer. It gets the actual pixels of your view, so "does this fillet look right to you" is a question it can look at rather than infer from the source. Ask it to frame something first if the answer depends on the angle.

A tool that needs the browser fails immediately if no page is open, with a message telling the agent to ask you to open the URL, rather than hanging until it times out.

## Pausing the agent's view control

**Pause** in the top bar refuses everything in the "driving" and "writing" lists above until you press it again. Use it when you are lining up a view you want to keep, or typing a comment against a pin, and you do not want the camera moving or a part disappearing underneath you.

This is the only control over the driving tools, since those never produce an approval card, so it is worth knowing it is there rather than discovering it when the camera moves at an awkward moment.

While paused the agent can still look, which is deliberate: reading does no harm and it is better informed when you unpause. It is told, in the refusal, that you have paused it and to ask you when it needs the view again, so it says so instead of retrying in a loop.

The pause is held by the server, not by the page, so it applies to the agent no matter which browser you set it in and it shows as pressed in every tab you have open. A tab you open afterwards shows it pressed too.

## Approving what the agent does

The split is by consequence, not by tool:

**Runs without asking.** A shell command the sandbox judges confined to the project folder. Reading and writing files inside the folder, running your generator, listing things. This is the common case and prompting for it would make the tool unusable.

**Asks first.** Editing or writing a file through the agent's own file tools, anything reaching the network, any command the sandbox cannot confine, and the three viewer tools that leave a file behind. You get a card naming the tool and showing its full arguments, with a multi-line command or file content laid out as text rather than crammed into one JSON line, because this is the thing you are being asked to read.

Three buttons:

- **Allow**, this one call.
- **Always allow**, this tool for the rest of the session, and remembered in `.mesh/permissions.toml` for future runs in this directory. Never offered for `Bash`, and refused for it even if something asks: one careless click should not become a standing grant of shell access.
- **Deny**, with an optional reason, which the agent receives verbatim. "Not that file, change the enclosure instead" is more useful to it than a bare refusal.

A card stays on screen, dimmed, while your decision is in flight, and disappears when the server confirms it. If you have the same card open on a phone and a laptop and answer on both, the second one is told its decision did not apply and what happened instead, rather than silently appearing to work.

If nothing is left to answer a request, because every browser closed, the request is denied and the agent is told why. Requests also expire after five minutes.

## Sessions

Each run gets a session, recorded under `.mesh/sessions/`, holding the event log for the conversation.

    annealage-mesh ./build -c              # continue the most recent session here
    annealage-mesh ./build -r              # list this directory's sessions and exit
    annealage-mesh ./build -r 20260812T…   # resume that one

`-r` takes its session id from the next token unconditionally, so `annealage-mesh -r ./build` reads `./build` as a session id. Put the directory first, or write `--resume=SID`.

Reloading the browser mid-turn loses nothing: the conversation belongs to the session rather than to the socket, so the page replays what it missed and carries on streaming. Closing the browser entirely and reopening it does the same.

One server per directory at a time. A second one refuses to start and tells you what the first is serving, because two agents resuming one session, or two writers on one event log, is corruption rather than an inconvenience.

## Reviewing from a phone

Below about 900 px wide the panes become tabs: Model, Review, Chat. Everything works, including pins and approval cards.

<img src="mesh-phone-chat.png" alt="The narrow layout: Model, Review and Chat as tabs, with an approval card for a Write awaiting a decision" width="380">


To reach it from a phone, the simplest safe route is Tailscale:

    annealage-mesh ./build --host tailscale

That resolves this host's tailnet address and binds only that, which is why it is an alias rather than something you assemble yourself. The banner prints the URL to open, token included. If you front it with `tailscale serve` or another reverse proxy, the browser's `Origin` will be that proxy's name, so pass it with `--origin https://name.ts.net`.

You can also bind an address directly, including `0.0.0.0` for every interface. Mesh does not stop you and does not hide it behind a warning flag, but understand what changes: on a non-loopback bind the per-run token is no longer defence in depth, it is the control, and anyone who has the URL has the agent.

## Reaching it through tailscale serve

`--host tailscale` binds your tailnet address directly, which is the simplest way to look at a part from your phone. `tailscale serve` is the other way, and it needs one extra flag.

`tailscale serve` terminates TLS itself and proxies to a local port, so the browser's `Origin` is `https://<machine>.<tailnet>.ts.net` while the server is bound to loopback and knows nothing about that name. Both the `Origin` check and the `Host` check would refuse it, correctly, because from the server's point of view it is a request from a name it never heard of.

Tell it the name:

    annealage-mesh ./part --origin https://your-machine.your-tailnet.ts.net &
    tailscale serve --bg --https=443 http://127.0.0.1:8765

Then open the `https://` URL, with the token fragment from the startup banner appended. `--origin` is repeatable if more than one name fronts it.

## Trusting a folder's Claude configuration

A directory can carry files the `claude` CLI obeys: `.claude/settings.json`, `.claude/settings.local.json`, scripts under `.claude/hooks/`, and `.mcp.json`. Those can grant tool permissions without asking you, and they can declare hooks, which are shell commands, one kind of which runs when the session opens, before any prompt is sent. Downloading and unpacking someone else's model archive is an ordinary thing to do, so a folder is not automatically trusted.

An ordinary folder of STL files has none of those files and you will never see this. When one does, agent mode refuses to start:

    error: /home/you/pack carries Claude configuration that this run has not been told to trust.
        /home/you/pack/.claude/settings.json
      Those files can declare hooks, which are shell commands the agent's CLI runs,
      and a session-start hook runs before any prompt is sent. Review them, then:
        accept them for this directory : annealage-mesh --trust-project-config
        or run the viewer with no agent : annealage-mesh --no-agent

Read the files, then accept them with `--trust-project-config`. Acceptance is recorded in your own configuration directory, not in the folder, against a digest of the exact content you reviewed. Any later change to those files asks again, including a change the agent itself makes.

The same digest is checked before every tool call, so if that configuration changes while the session is running, every subsequent call is refused and the chat pane tells you. Restart to review the change.

`CLAUDE.md` is deliberately not part of this. It cannot execute, and every tool call it might provoke still faces the sandbox and your approval. Gating it would prompt about nearly every real project and teach you to accept without reading, which costs more than it buys.

## Files in your directory

| Path | What it is |
| --- | --- |
| `mesh-comments.json` | Your pins, rewritten on each Submit. |
| `mesh-comments.log` | Every Submit ever, appended, one JSON object per line. |
| `mesh-callouts.json` | The agent's callouts, written by its own tool or by hand. Either way the viewer updates live. |
| `images/` | Pictures you attached, sketches you drew, and screenshots the agent saved. Served back at `/asset/<name>`, and meant to be committed. |
| `*.stl` | Your parts. Added, regenerated or deleted, the viewer follows within a fraction of a second. |
| `.mesh/sessions/` | One directory per session, holding its event log. |
| `.mesh/permissions.toml` | Tools you chose "Always allow" for. Plain TOML, safe to edit or delete. |
| `.mesh/lock` | Held while a server is running here. Stale locks are reclaimed automatically. |

Only `.stl` files are served, and only ones that are regular files inside the served tree: symlinks are refused rather than followed, so a link cannot expose a file from outside the directory you chose to share.

## Every flag

| Flag | Meaning |
| --- | --- |
| `dir` | Directory of STL files to serve. Defaults to the current directory. |
| `--port PORT` | TCP port. Default 8765. |
| `--host HOST` | Bind address: an IP, a resolvable name, `0.0.0.0`, or `tailscale`. Default `127.0.0.1`. |
| `--origin ORIGIN` | An additional allowed browser `Origin`. Repeatable. For reverse proxies. |
| `--token TOKEN` | Use this token instead of a generated one. |
| `--no-open` | Do not open a browser. |
| `--model MODEL` | Model for the agent. Defaults to whatever your `claude` CLI is set to. |
| `--effort LEVEL` | `low`, `medium`, `high`, `xhigh` or `max`. How much thinking per turn. |
| `--permission-mode MODE` | `default`, `acceptEdits` or `plan`. `bypassPermissions` is not offered. |
| `--no-agent` | Viewer only, the same thing the `view` subcommand does. |
| `--no-git` | Do not run `git init` and do not make the scaffold commit. |
| `--settings` | Print every setting with the layer it came from, then exit. |
| `--trust-project-config` | Accept this directory's Claude configuration. |
| `-c`, `--continue` | Continue the most recent session for this directory. |
| `-r [SID]`, `--resume [SID]` | Resume `SID`; with no id, list sessions and exit. |
| `--version` | Print the version and exit. |

And the three subcommands, each taking the same `dir` positional:

| Command | Meaning |
| --- | --- |
| `view [DIR]` | Viewer only: no agent, no scaffolding, no git, no lock. Several may run against one directory. |
| `init [DIR]` | Scaffold plus git, then exit. Takes `--no-git` and `--force`. |
| `doctor [DIR]` | Print what this machine has and what this project looks like, then exit. |

A subcommand is only recognised as the first argument, so a directory of models called `view` is still servable as `./view`. When `CLAUDECODE` is set in the environment, which is the case in any shell Claude Code starts, the bare form flips to viewer-only and prints a note saying so, rather than starting an agent inside an agent.

## When something is wrong

**The chat pane says Unavailable.** The `claude` CLI could not start or is not authenticated. The pane shows the child's own error output, which is usually specific. The viewer and Submit keep working meanwhile.

**Agent mode exits with "needs bubblewrap and socat".** Install them (`apt install bubblewrap socat`), or run `--no-agent` for the viewer alone. Mesh refuses rather than running the agent's shell uncontained.

**The posture line says the sandbox is not active.** A dependency was found missing after startup. The agent still works, but bash asks for approval instead of running contained. The line names what is missing.

**The port is in use.** Another server, possibly another Mesh, has it. Pass `--port`.

**"It is serving:" and a URL.** A Mesh is already running for this directory. Open the URL it printed, or stop the other one.

**A model does not appear.** It must be a regular `.stl` file inside the served tree. Symlinks are refused, and files under dot-directories are skipped. A part the agent has just generated should show up within about a second; if it does not, check the connection pill in the top bar, since the update is pushed over the WebSocket and a page showing "Reopen URL" is not receiving pushes at all.

**A regenerated part still looks like the old one.** Same cause: the push needs a live connection. Reopen the URL printed in the terminal.

**The page says it is reconnecting.** The server went away or the network dropped. It reconnects on its own and replays what it missed; callouts fall back to polling while the socket is down so the view stays current either way.

**Approval cards keep appearing for the same thing.** Use **Always allow** to remember that tool for this directory. It is stored in `.mesh/permissions.toml`, and deleting a line there asks again.
