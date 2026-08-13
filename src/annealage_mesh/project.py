"""Project scaffolding: the folders and generated files a served directory
needs, and putting that directory under git.

A served directory does not have to have existed before this module heard of
it. Running ``annealage-mesh`` in an empty folder scaffolds it; running it
again in a folder that already has everything verifies that and changes
nothing. Both are the same call, ``ensure_project``, because there is no
separate "has this project been set up" question anywhere else that would
need a second answer to agree with the first.

Only two of the things this module writes have content worth reconciling
across runs: ``.gitignore`` and ``CLAUDE.md``. The two scaffold directories,
``models`` and ``images``, are either created empty or found already
populated; there is nothing about an existing one a rerun could need to
fix, so ``force`` has no meaning for a directory and only ever applies to
the two generated files.

Git is handled here rather than left to the human, because "an agent with
shell access is about to run in this folder" is exactly the situation where
being able to see what changed, and to undo a change, matters most, and a
folder with no version control offers neither. What this module deliberately
does not do is commit anything after the first scaffold commit: that commit
exists so an agent session never starts against a folder with no history to
diff against, not so this module keeps committing on the human's behalf.
Whether the folder is already a repository, sits inside one, or has no git
binary to ask at all are told apart rather than folded into one skip, because
each is a different fact for a human to act on: the first needs nothing
further, the second means running ``git init`` here would be the wrong move
regardless of what this module thinks, and the third means installing git is
what is missing, not anything about the folder.

Every git-shaped decision goes through ``run`` and ``which``, injected the
way ``net.py`` injects them for its own subprocess calls, and never through
looking at ``project_dir`` for a ``.git`` entry directly: git's own idea of
whether a directory is a repository, or sits inside one, is the one that has
to match reality, and a filesystem shortcut that happened to agree with it
today is exactly the kind of check that starts lying the day someone's
workflow does something git supports and a bare directory listing does not
(a linked worktree, a ``.git`` file rather than a directory, ``GIT_DIR`` set
in the environment). Driving the whole decision through ``run`` also means
every test below can pin it with canned subprocess output and never has to
create, or even simulate the shape of, an actual repository.
"""

import dataclasses
import shutil
import subprocess
from pathlib import Path

from . import paths

# The two directories a served project needs. "images" is spelled from
# paths.IMAGES_DIRNAME rather than repeated as a literal, so the name the
# asset route serves from and the name this module creates can never drift
# apart from each other.
SCAFFOLD_DIRS = ("models", paths.IMAGES_DIRNAME)

GITIGNORE_NAME = ".gitignore"
CLAUDE_MD_NAME = "CLAUDE.md"


@dataclasses.dataclass(frozen=True)
class GitResult:
    """What happened when ``ensure_project`` looked at git for one project.

    ``initialised`` and ``committed`` together with ``skipped_reason`` pick
    out exactly one of four shapes, and a caller branches on the two
    booleans, never on the text of ``skipped_reason``, which exists to be
    printed rather than matched:

        (False, False, "git is not installed")
        (False, False, "already a git repository")
        (False, False, "already inside a git repository")
        (True,  False, "git user.email is unset, so nothing was committed")
        (True,  True,  None)

    ``committed`` is never true while ``initialised`` is false: a commit is
    only attempted immediately after ``git init`` has just succeeded in the
    same call, never against a repository this call found already there.
    """

    initialised: bool
    committed: bool
    skipped_reason: str | None = None


@dataclasses.dataclass(frozen=True)
class ScaffoldResult:
    """What one ``ensure_project`` call found or did, for a CLI to print and
    a test to assert against without re-deriving it from the filesystem.

    ``created``, ``kept`` and ``regenerated`` list project-relative names
    drawn from ``SCAFFOLD_DIRS``, ``GITIGNORE_NAME`` and ``CLAUDE_MD_NAME``;
    each name appears in exactly one of the three. ``regenerated`` holds a
    name only when ``ensure_project`` was called with ``force=True`` and that
    name already existed, since neither ``created`` nor ``kept`` describes
    overwriting something that was already there. ``git`` is ``None`` when
    ``ensure_project`` was called with ``git=False``, and a ``GitResult``
    otherwise.
    """

    created: tuple[str, ...] = ()
    kept: tuple[str, ...] = ()
    regenerated: tuple[str, ...] = ()
    git: GitResult | None = None


def gitignore_body():
    """The generated ``.gitignore``'s exact contents.

    ``.mesh/`` is ignored as a whole except its own ``config.toml``, which a
    project is meant to commit and share: it holds no secret, since the
    per-run token lives in ``.mesh/lock`` and is regenerated every start,
    while ``.mesh/state.json``, ``.mesh/permissions.toml`` and
    ``.mesh/sessions/`` hold session and machine-local state nobody else's
    checkout needs.

    Neither scaffold directory is ignored: an STL a print was made from, and
    the photograph taken of the result, are the evidence a review depends
    on. Both can grow large, which the trailing comment names without
    configuring git-lfs; setting that up is left to whoever's project
    actually needs it.
    """
    return (
        "# Generated by annealage-mesh init. Left alone on a later run\n"
        "# unless init is given --force.\n"
        "\n"
        ".mesh/\n"
        "!.mesh/config.toml\n"
        "\n"
        "# models/ and images/ stay tracked: STL/3MF inputs and\n"
        "# agent-generated output, and uploads, sketch composites and\n"
        "# captured views, are the evidence a review depends on.\n"
        "#\n"
        "# Both can grow large. git-lfs (https://git-lfs.com) is worth\n"
        "# setting up for them on a project with many or big binaries;\n"
        "# this file does not configure it.\n"
    )


def claude_md_body(project_dir):
    """The generated ``CLAUDE.md``'s exact contents for the project rooted at
    ``project_dir``.

    Written for whichever agent finds itself working in this folder, stating
    the same exchange-file contract the published skill documents: where a
    human's submitted pins land, where an agent's own callouts belong, and
    that a chat pane driving this same folder may already be attached. An
    agent that opens this file with no memory of any skill still knows the
    shape of the folder it is standing in.
    """
    name = Path(project_dir).resolve().name
    models_dir = SCAFFOLD_DIRS[0]
    paragraphs = [
        "# %s" % name,
        "This folder is served by `annealage-mesh`, a local viewer for the "
        "STL and 3MF files under `%s/`, with an optional chat pane beside it "
        "where an agent can work in this same folder while a human watches "
        "the model update live." % models_dir,
        "## Layout",
        "- `%s/` holds the model files the viewer indexes, including "
        "anything regenerated while an agent session is attached; the "
        "viewer picks up a changed file with no restart." % models_dir,
        "- `%s/` holds uploads, sketch composites and captured views, meant "
        "to be committed as evidence of what a part looked like at some "
        "point in review." % paths.IMAGES_DIRNAME,
        "- `%s` and `%s` hold the human's pinned comments, written by the "
        "viewer when a pin is submitted; re-read the `.json` file after "
        "asking for feedback." % (paths.COMMENTS_JSON_NAME, paths.COMMENTS_LOG_NAME),
        "- `%s` holds callouts an agent writes to point the human at a "
        "location on the model; the whole file is rewritten each time, "
        "never merged." % paths.CALLOUTS_JSON_NAME,
        "- `review/` holds exported transcripts; it does not exist until "
        "the first one is exported.",
        "## Mesh tools",
        "When an agent session is attached, the `mesh` MCP server exposes "
        "tools for the camera, part visibility, pins, callouts, measurement, "
        "screenshots and transcript export, all acting on the same viewer "
        "the human is looking at. Prefer them over hand-editing the files "
        "above while a session is attached: they keep the viewer in sync "
        "with no extra step.",
    ]
    return "\n\n".join(paragraphs) + "\n"


def _scaffold_file(project_dir, name, body, force, created, kept, regenerated):
    """Write ``body`` to ``project_dir``/``name`` unless it already exists
    and ``force`` is false, in which case it is left untouched, byte for
    byte, so a human's own edits to a generated file never get read back at
    all, let alone rewritten.

    Writes through ``paths.atomic_replace`` rather than a plain open: that
    function moves a temporary file into place with ``os.replace``, which
    replaces the directory entry named ``name`` outright rather than
    following it, so a symlink planted at that name (a hazard the project
    directory a human points ``init`` at was not necessarily created for
    this purpose) is replaced rather than written through to whatever it
    pointed at.
    """
    target = project_dir / name
    existed = target.exists()
    if existed and not force:
        kept.append(name)
        return
    paths.atomic_replace(target, body.encode("utf-8"))
    (regenerated if existed else created).append(name)


def _probe_git_state(project_dir, *, run):
    """Return ``"root"``, ``"nested"`` or ``"clear"`` for what git, asked
    through ``run`` alone, already knows about ``project_dir``.

    ``"root"`` is a folder that is itself a repository's top level, whether
    from an earlier scaffold or a clone; ``"nested"`` is a folder inside a
    repository rooted at some ancestor, which a second ``git init`` would not
    usefully fix and this function never attempts. Two git invocations
    rather than one, because ``--is-inside-work-tree`` alone cannot tell
    those two states apart: it reports ``true`` for both, and only
    ``--show-toplevel`` says which directory the repository it found is
    actually rooted at.
    """
    inside = run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=project_dir,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if getattr(inside, "returncode", 1) != 0 or (inside.stdout or "").strip() != "true":
        return "clear"
    top = run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=project_dir,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if getattr(top, "returncode", 1) != 0:
        return "nested"
    try:
        is_root = Path((top.stdout or "").strip()).resolve() == project_dir
    except OSError:
        is_root = False
    return "root" if is_root else "nested"


def _ensure_git(project_dir, *, run, which):
    """Run the git side of ``ensure_project`` and return the ``GitResult``.

    ``project_dir`` is not touched by anything here except through ``run``:
    the git binary itself decides, in every branch below, whether a
    directory is a repository, sits inside one, or is clear to initialise,
    and this function only ever acts on what it was told.
    """
    if not which("git"):
        return GitResult(False, False, "git is not installed")

    state = _probe_git_state(project_dir, run=run)
    if state == "root":
        return GitResult(False, False, "already a git repository")
    if state == "nested":
        return GitResult(False, False, "already inside a git repository")

    init = run(
        ["git", "init"], cwd=project_dir, capture_output=True, text=True, timeout=10, check=False
    )
    if getattr(init, "returncode", 1) != 0:
        raise RuntimeError(
            "git init failed in %s: %s" % (project_dir, (init.stderr or init.stdout or "").strip())
        )

    email = run(
        ["git", "config", "user.email"],
        cwd=project_dir,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if getattr(email, "returncode", 1) != 0 or not (email.stdout or "").strip():
        return GitResult(True, False, "git user.email is unset, so nothing was committed")

    run(
        ["git", "add", "-A"],
        cwd=project_dir,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    commit = run(
        ["git", "commit", "--quiet", "-m", "Scaffold the mesh project"],
        cwd=project_dir,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if getattr(commit, "returncode", 1) != 0:
        raise RuntimeError(
            "git commit failed in %s: %s"
            % (project_dir, (commit.stderr or commit.stdout or "").strip())
        )
    return GitResult(True, True, None)


def ensure_project(project_dir, *, git=True, force=False, run=subprocess.run, which=shutil.which):
    """Create or verify the scaffold ``project_dir`` needs to be served, and
    put it under git unless told not to.

    Idempotent: a second call over a folder this already scaffolded writes
    nothing and reports every file and directory it found already in place
    through ``kept``. ``force`` regenerates the two generated files,
    ``.gitignore`` and ``CLAUDE.md``, when they already exist; it has no
    effect on the two scaffold directories, which are never rewritten once
    present, and no effect on git, which this function never re-initialises
    or re-commits once a repository already exists at or above
    ``project_dir``.

    ``git=False`` skips the git side entirely and leaves ``result.git`` as
    ``None``, which is how ``--no-git`` is meant to read: "nothing was even
    attempted", distinct from every outcome ``_ensure_git`` can report for a
    call that did attempt it.
    """
    project_dir = paths.resolve_serve_dir(project_dir)
    project_dir.mkdir(parents=True, exist_ok=True)

    created = []
    kept = []
    regenerated = []

    for name in SCAFFOLD_DIRS:
        target = project_dir / name
        if target.exists():
            kept.append(name)
        else:
            target.mkdir(parents=True)
            created.append(name)

    _scaffold_file(project_dir, GITIGNORE_NAME, gitignore_body(), force, created, kept, regenerated)
    _scaffold_file(
        project_dir, CLAUDE_MD_NAME, claude_md_body(project_dir), force, created, kept, regenerated
    )

    git_result = _ensure_git(project_dir, run=run, which=which) if git else None

    return ScaffoldResult(
        created=tuple(created),
        kept=tuple(kept),
        regenerated=tuple(regenerated),
        git=git_result,
    )
