"""Whether the served directory's Claude configuration may take effect.

A directory can carry configuration that the `claude` CLI obeys: `.claude/settings.json`, `.claude/settings.local.json`, scripts under `.claude/hooks/`, and `.mcp.json`. Settings files may declare hooks, hooks are shell commands, and a `SessionStart` hook runs when the session opens, before any prompt is sent and before any permission callback exists to consult. A `.mcp.json` names server commands to spawn. So configuration in this directory is executable code, and the directory is not necessarily the human's own work: pointing Mesh at an unpacked download is an ordinary thing to do.

The CLI's own defence against this is its workspace trust dialog, and it states in `--print`'s help that the dialog "is skipped when Claude is run in non-interactive mode", which is the mode every SDK session runs in, adding: "Only use this in directories you trust." That makes the trust decision the embedding application's to make, and this module is Mesh's.

Two controls are built from what is here, and they answer different threats.

**The startup gate** (``cli.py``) refuses agent mode when this directory carries configuration whose current content the human has not accepted. It has to run before the session opens, because a `SessionStart` hook executing is precisely what it prevents, and nothing inside a running session is early enough.

**The in-session tripwire** (``session/sdk.py``) recomputes ``config_digest`` on every tool call and denies the call if it no longer matches what the gate accepted. It exists because the CLI re-reads these files while the session runs: a settings file written mid-session takes effect immediately, so a directory that was trustworthy at startup does not stay trustworthy by itself. The tripwire watches the files rather than the ways they might be written, so it is indifferent to whether a write arrived through Write, Edit, a shell redirect, or a `git checkout`.

Trust records live in the user's own configuration directory, never in the served directory, because a record stored alongside the thing it vouches for could be shipped inside the download it is meant to vouch for.

What is deliberately not gated is `CLAUDE.md`. It is instructions rather than executable configuration, so a hostile one is a prompt-injection vector and not a code-execution one, and every tool call it might provoke still reaches the sandbox and the human. Gating it would prompt about nearly every real project and teach the human to accept without reading, which costs more than it buys.
"""

import hashlib
import os
import sys
from pathlib import Path
from typing import Iterable, Optional, Tuple

# Every path, relative to the served directory, whose content the CLI may
# execute or obey. Files and directories both: `.claude/hooks` holds scripts a
# trusted settings file may invoke by name, so a change to one of those scripts
# changes what the directory does without changing any settings file.
GUARDED_PATHS = (
    ".claude/settings.json",
    ".claude/settings.local.json",
    ".claude/hooks",
    ".mcp.json",
)

# Beyond this many bytes in one guarded file, the digest covers the file's size
# rather than its content. Reached only by something that is not configuration,
# and the size still changes when the file does, so the gate and the tripwire
# both still notice a rewrite.
_MAX_DIGEST_BYTES = 1 << 20

# The digest of a directory carrying no guarded configuration at all. Named so
# callers can say "nothing to trust" without recomputing it, and distinct from
# any real digest because no file list produces an empty hash input.
EMPTY_DIGEST = "empty"

_TRUST_FILE = "trusted-projects"


def present(root) -> Tuple[Path, ...]:
    """The guarded paths that exist in ``root``, in ``GUARDED_PATHS`` order.

    Uses ``lstat`` semantics via ``Path.exists`` on the entry itself, so a
    symlink that dangles counts as absent while one that resolves counts as
    present, matching what the CLI would find when it tried to read it.
    """
    root = Path(root)
    return tuple(root / rel for rel in GUARDED_PATHS if (root / rel).exists())


def config_digest(root) -> str:
    """A digest of every guarded path's identity and content under ``root``.

    ``EMPTY_DIGEST`` when the directory carries none of them, which is the
    common case for a plain folder of STL files and the case that needs no
    trust decision at all.

    Covers three things beyond file content, each because changing it changes
    what the directory does while leaving content equal: which paths exist, so
    deleting one is a change; whether each is a symlink and to where, so
    re-pointing a link at different configuration is a change; and, for
    ``.claude/hooks``, every file beneath it by relative path, so adding a
    script is a change.

    Unreadable is recorded as its own state rather than skipped: a file the
    CLI might read and this cannot has to compare unequal to one that was
    read, or the gate would accept a directory it never actually inspected.
    """
    root = Path(root)
    parts = []
    for rel in GUARDED_PATHS:
        path = root / rel
        if not path.exists():
            continue
        for name, blob in _entries(path, rel):
            parts.append(b"%s\0%d\0%s" % (name.encode("utf-8"), len(blob), blob))
    if not parts:
        return EMPTY_DIGEST
    digest = hashlib.sha256()
    for part in parts:
        digest.update(b"%d\0" % len(part))
        digest.update(part)
    return digest.hexdigest()


def _entries(path: Path, rel: str) -> Iterable[Tuple[str, bytes]]:
    """One ``(name, bytes)`` pair per file the CLI could read at ``path``.

    A symlink contributes its target rather than its target's content, so the
    link and the file it points at are distinguishable, and the target's own
    content arrives through the read that follows it.
    """
    if path.is_symlink():
        try:
            target = os.readlink(path)
        except OSError as exc:
            yield rel + " (symlink)", b"unreadable:%r" % (exc,)
            return
        yield rel + " (symlink)", target.encode("utf-8", "surrogateescape")
    if path.is_dir():
        for child in sorted(p for p in path.rglob("*") if p.is_file()):
            yield "%s/%s" % (rel, child.relative_to(path).as_posix()), _read(child)
        return
    yield rel, _read(path)


def _read(path: Path) -> bytes:
    try:
        size = path.stat().st_size
        if size > _MAX_DIGEST_BYTES:
            return b"oversized:%d" % size
        return path.read_bytes()
    except OSError as exc:
        return b"unreadable:%r" % (exc,)


# ---------------------------------------------------------------------------
# The trust record.
# ---------------------------------------------------------------------------


def config_home() -> Path:
    """The user's configuration directory, per platform convention."""
    override = os.environ.get("XDG_CONFIG_HOME")
    if override:
        return Path(override)
    if os.name == "nt":
        appdata = os.environ.get("APPDATA")
        if appdata:
            return Path(appdata)
    elif sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support"
    return Path.home() / ".config"


def trust_store_path() -> Path:
    return config_home() / "annealage-mesh" / _TRUST_FILE


class TrustStore:
    """Which directories' configuration the human has accepted, and as of what content.

    One record per line, ``<digest> <absolute path>``, split once from the
    left so a path containing spaces survives a round trip. A line this cannot
    parse is dropped rather than raising, and a missing or unreadable file
    reads as no records: every failure here costs one re-acceptance, which is
    the direction that asks more rather than less.
    """

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path is not None else trust_store_path()

    def _records(self):
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except OSError as exc:
            sys.stderr.write(
                "warning: could not read %s: %r; the served directory's Claude "
                "configuration will need accepting again\n" % (self.path, exc)
            )
            return {}
        records = {}
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            digest, sep, target = line.partition(" ")
            if sep and target.strip():
                records[target.strip()] = digest
        return records

    def accepted(self, root, digest: str) -> bool:
        """Whether ``root``'s configuration is recorded as accepted at exactly ``digest``."""
        return self._records().get(str(Path(root).resolve())) == digest

    def accept(self, root, digest: str) -> None:
        """Record ``root``'s configuration as accepted at ``digest``, replacing any earlier record for it."""
        records = self._records()
        records[str(Path(root).resolve())] = digest
        lines = [
            "# Directories whose Claude configuration you have accepted (annealage-mesh).",
            "# One record per line: <digest> <absolute path>. Deleting a line asks again.",
        ]
        lines.extend("%s %s" % (records[target], target) for target in sorted(records))
        lines.append("")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text("\n".join(lines), encoding="utf-8")
        tmp.replace(self.path)


# ---------------------------------------------------------------------------
# The gate, as a decision the CLI renders.
# ---------------------------------------------------------------------------


def refusal_message(root, entries: Iterable[Path]) -> str:
    """What to tell a human whose directory carries configuration they have not accepted.

    Names the files, says what they can do rather than only what they contain,
    and offers both ways forward, because a refusal that does not say how to
    proceed gets worked around by whatever the human tries next.
    """
    listed = "\n".join("    %s" % path for path in entries)
    return (
        "error: %s carries Claude configuration that this run has not been told to trust.\n"
        "%s\n"
        "  Those files can declare hooks, which are shell commands the agent's CLI runs,\n"
        "  and a session-start hook runs before any prompt is sent. Review them, then:\n"
        "    accept them for this directory : annealage-mesh --trust-project-config\n"
        "    or run the viewer with no agent : annealage-mesh --no-agent\n"
        "  Acceptance is recorded per directory against the exact content reviewed, so\n"
        "  any later change to these files asks again.\n" % (root, listed)
    )
