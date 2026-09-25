"""Mesh's layout of a served directory: the exchange files, the model scan and
index, and the index of the packaged static tree.

This module has no HTTP awareness: it knows about a served directory and the
files under it, not requests or responses. ``app.py`` and
``http/routes_viewer.py`` turn what this module produces into HTTP
responses. The path-safety primitives underneath (``safe_join``, the guarded
fixed-file reads and appends, atomic replacement, image and transcript
creation) are not Mesh's own and live in ``agent/files.py``, so the agent
layer and every product share one implementation of them.

Files Mesh exchanges with an agent, by fixed name in the served directory:
    mesh-callouts.json   agent-authored callouts, read whole by
                         ``files.read_fixed_file``
    mesh-comments.log    opened once by ``files.open_fixed_file_for_append``
    mesh-comments.json   human submissions, replaced atomically by the caller
"""

import os
import stat
import sys
from collections import Counter
from pathlib import Path

from .agent.files import resolve_serve_dir

COMMENTS_JSON_NAME = "mesh-comments.json"
COMMENTS_LOG_NAME = "mesh-comments.log"
CALLOUTS_JSON_NAME = "mesh-callouts.json"

# Minimal extension -> content-type map (stdlib mimetypes misses .stl/.3mf).
# Used for the packaged viewer, model bytes and callouts.json, none of
# which are files an outside party can place into the served directory
# under a name of their choosing; a served directory's images/ subtree is,
# so /asset uses agent/files.py's ASSET_CONTENT_TYPES instead of this map.
CONTENT_TYPES = {
    ".stl": "application/vnd.ms-pki.stl",
    ".3mf": "model/3mf",
    ".step": "application/step",
    ".stp": "application/step",
    ".json": "application/json",
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".svg": "image/svg+xml",
}


# The manifest scan indexes files with these extensions, at any depth under
# the served directory. See scan_models for the walk itself and for why each
# exclusion rule (dotfiles, symlinks, hardlinks) applies uniformly at every
# depth rather than only at the top.
MODEL_EXTENSIONS = {".stl"}

# Cap on the number of files one scan will index. Past this many matching
# files, the scan stops early: files beyond the cap are simply absent from
# the manifest (and therefore unreachable via /model or the .stl alias)
# until the directory is pruned below the cap or the cap is raised. This
# bounds one scan's cost on a served directory of unbounded size; it is not
# a security control.
MAX_INDEXED_FILES = 5000

# Depth cap for the recursive model scan, counting the served directory
# itself as depth 0. A directory beyond this depth is not opened. The served
# directory is arbitrary, attacker-controllable input (a reviewed bundle, a
# git clone, a zip extracted by hand), so nothing stops it from containing a
# real directory nested far deeper than any sane project layout, whether by
# accident (a build tool's output copied in whole) or on purpose (padding
# the walk to cost this process as much CPU and stack as the attacker likes,
# with no symlink needed to do it: this is real directories, which the
# symlink refusal above has no bearing on). Twelve levels is far more than a
# models/ tree of real subassemblies needs.
MAX_SCAN_DEPTH = 12

# Cap on the number of directories one scan will open (os.scandir calls),
# independent of MAX_SCAN_DEPTH: a tree can be shallow and still enormous, a
# single directory holding thousands of subdirectories rather than a long
# chain of them. Past this many opened directories the scan stops
# descending entirely; whatever it already found stays in the manifest, and
# truncated is set so the listing is understood to be partial rather than
# complete.
MAX_SCAN_DIRS = 2000

# Extensions the packaged static/ tree may serve, plus the one extensionless
# name carved out below. This is an allowlist, not a denylist: a file with
# any other extension sitting in static/ (an editor's ".bak", a source
# map's ".map", a stray ".py") is simply invisible to the scan and therefore
# unreachable under /static, regardless of what it contains, so dropping a
# file into that directory can never make it servable by accident.
STATIC_EXTENSIONS = {".html", ".css", ".js", ".json"}

# The vendored three.js licence file ships as a bare "LICENSE" with no
# extension, inside static/js/vendor/. It is carved out by name and by
# directory rather than added to STATIC_EXTENSIONS itself, so the allowlist
# above stays a pure extension test everywhere else in the tree and this one
# exception cannot be widened by accident to any extensionless file anywhere
# under static/.
STATIC_LICENSE_DIRNAME = "vendor"
STATIC_LICENSE_FILENAME = "LICENSE"

# Cap on the number of files the static asset index will hold. static/ is a
# fixed tree shipped inside this package, not a directory a served project
# can grow, so 500 is ample headroom over the real file count with no
# expectation of ever being reached; unlike MAX_INDEXED_FILES, hitting it
# would mean something is wrong with the install, not with someone's
# project, so it is worth a warning rather than a silent truncation.
MAX_STATIC_FILES = 500


def comments_path(serve_dir):
    return resolve_serve_dir(serve_dir) / COMMENTS_JSON_NAME


def comments_log_path(serve_dir):
    return resolve_serve_dir(serve_dir) / COMMENTS_LOG_NAME


def callouts_path(serve_dir):
    return resolve_serve_dir(serve_dir) / CALLOUTS_JSON_NAME


def scan_models(serve_dir):
    """Recursively scan ``serve_dir`` for indexable model files.

    Returns ``(models, truncated)``. ``models`` is a list of dicts sorted by
    ``rel``, each with:

        name        file stem, e.g. "widget"; may repeat across entries
                    once the scan descends, since two directories can each
                    hold a file with the same stem
        file        bare filename, e.g. "widget.stl"; may also repeat
        path        absolute filesystem path, as a string
        rel         POSIX-style path relative to serve_dir, joined with "/"
                    regardless of the host OS's own separator; this is the
                    one field guaranteed unique across the listing, and it
                    is the key /model/<rel> resolves through
        label       a short, unique display string; see the module-level
                    ``_compute_labels`` for how it is derived from ``rel``

    The walk is an explicit stack over ``os.scandir``, not ``os.walk``:
    ``os.walk`` decides what to do with a name after it has already been
    turned into a plain string, which throws away the ``DirEntry`` a
    same-syscall ``lstat`` needs, and its own symlink handling is a
    default a caller must remember to override rather than a decision this
    code makes for itself at each entry.

    A path component starting with "." is skipped for both files and
    directories, in one rule rather than a name-by-name denylist: it excludes
    ``.git``, ``.mesh``, a stray ``.mesh-comments-*.tmp``, and anything else
    of the same shape without needing to know its name in advance. A
    dotdir's contents are therefore never visited at all, not merely
    filtered out of the results afterward.

    A directory that is a symlink is not descended into and not listed, for
    the same reason a symlinked file is refused below: deciding where a link
    points means resolving it, resolving is several lookups, and anything
    able to write into this directory can change what the name points at
    between them, so a link aimed outside the tree could pass a check made
    against where it pointed a moment earlier. ``entry.stat(follow_symlinks=
    False)`` reports a symlink as ``S_ISLNK``, matching neither the
    directory nor the regular-file branch below, so this one check excludes
    a symlinked directory and a symlinked file alike without a separate
    ``is_symlink`` test.

    A file qualifies only if it is a regular file with exactly one hard
    link and a matching extension (case-insensitively). A second link means
    the bytes may belong to a file anywhere else on the filesystem, which
    the name alone cannot reveal, so serving them would publish a file
    outside the served directory; the existing stderr warning for that case
    is kept. ``entry.stat(follow_symlinks=False)`` is used rather than
    ``entry.is_file()`` because the link count is needed and a ``DirEntry``'s
    cached ``d_type`` does not carry it, so ``is_file()`` would still cost a
    separate stat for exactly the information this code already needs.

    Traversal order within each directory is sorted by name before dotfiles
    or extensions are even considered, and a directory's subdirectories are
    pushed onto the stack in reverse sorted order so they come off it (and
    so get opened) in forward sorted order. This does more than make the
    final list's order reproducible, which the trailing sort below would do
    on its own: it makes the *set* of files found before MAX_INDEXED_FILES
    or MAX_SCAN_DIRS cuts the walk short reproducible too, since which files
    are "first" depends on the order they were visited in, and the
    underlying OS gives no ordering guarantee of its own.

    ``truncated`` is True if any cap fired: MAX_INDEXED_FILES matching files
    were found, a directory was skipped for sitting deeper than
    MAX_SCAN_DEPTH, or MAX_SCAN_DIRS directories were opened. In every case
    it means the same thing to a caller: this listing is incomplete, not
    necessarily "there are more than MAX_INDEXED_FILES models".
    """
    serve_dir = resolve_serve_dir(serve_dir)
    models = []
    truncated = False
    dirs_opened = 0
    # Each stack entry is (absolute directory path, path segments from
    # serve_dir to that directory, depth). The root is depth 0 and is always
    # opened; MAX_SCAN_DEPTH only ever refuses a *child* of an already-opened
    # directory, so it bounds how far the walk can descend, not whether the
    # served directory itself is scanned.
    stack = [(serve_dir, (), 0)]
    while stack:
        dirpath, rel_parts, depth = stack.pop()
        if dirs_opened >= MAX_SCAN_DIRS:
            truncated = True
            break
        dirs_opened += 1
        try:
            children = sorted(os.scandir(dirpath), key=lambda e: e.name)
        except OSError:
            continue

        subdirs = []
        cap_hit = False
        for entry in children:
            if entry.name.startswith("."):
                continue
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISDIR(st.st_mode):
                subdirs.append(entry.name)
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            if Path(entry.name).suffix.lower() not in MODEL_EXTENSIONS:
                continue
            if st.st_nlink != 1:
                sys.stderr.write(
                    "warning: skipping %s: %d hard links, so its contents may be a "
                    "file outside the served directory\n" % (dirpath / entry.name, st.st_nlink)
                )
                continue
            if len(models) >= MAX_INDEXED_FILES:
                truncated = True
                cap_hit = True
                break
            fpath = dirpath / entry.name
            models.append(
                {
                    "name": Path(entry.name).stem,
                    "file": entry.name,
                    "path": str(fpath),
                    "rel": "/".join(rel_parts + (entry.name,)),
                    # Not advertised; the index moves these into a side table so
                    # a route can require the file it opens to be the one
                    # checked here.
                    "_dev": st.st_dev,
                    "_ino": st.st_ino,
                }
            )
        if cap_hit:
            break
        for name in sorted(subdirs, reverse=True):
            child_depth = depth + 1
            if child_depth > MAX_SCAN_DEPTH:
                truncated = True
                sys.stderr.write(
                    "warning: not descending into %s: more than MAX_SCAN_DEPTH "
                    "(%d) directories deep\n" % (dirpath / name, MAX_SCAN_DEPTH)
                )
                continue
            stack.append((dirpath / name, rel_parts + (name,), child_depth))

    models.sort(key=lambda m: m["rel"])
    labels = _compute_labels(models)
    for m in models:
        m["label"] = labels[m["rel"]]
    return models, truncated


def _compute_labels(models):
    """Return ``{rel: label}``, a short display string unique across ``models``.

    For one entry, candidate ``L_k`` is the last ``k`` slash-separated
    segments of its ``rel``, with the model extension stripped from the
    final segment only (directory segments never carry one to strip). The
    smallest ``k`` is chosen for which no *other* entry's own ``L_k``
    (computed the same way, capped to that entry's own segment count once
    ``k`` exceeds it) equals this one, so a model keeps the short, familiar
    form ("a/widget") unless something else in the tree needs more context
    to tell apart from it. ``k`` is tried up to the longest ``rel`` in the
    whole set, not just this entry's own length, because a short path can
    still be forced to a larger ``k`` purely to wait out a longer path that
    has not yet grown specific enough to stop colliding with it (an entry
    "x/foo.stl" is indistinguishable from "y/x/foo.stl" at k=1 and k=2, and
    only stops colliding once k=3 lets the second entry's own leading
    segment show).

    That search is done one ``k`` at a time across every entry still
    unresolved, not one entry at a time across every ``k``: for a given
    ``k``, two entries collide there exactly when their candidate strings are
    equal, which one pass building ``Counter(candidate -> count)`` decides
    for every remaining entry at once, in place of comparing each entry
    against every other entry at every ``k`` in turn.

    Resolving each entry against its own candidates in isolation is still
    not sufficient to make the whole set unique: two entries can each reach
    a ``k`` where nothing else matches at that ``k``, yet still land on the
    identical string, because their full segment sequences agree everywhere
    except the file extension ("a/b.stl" and "a/b.STL" produce the same
    "a/b" at every possible ``k``, since only the stripped-off extension
    differs), or because one entry's fallback string happens to equal
    another entry's chosen short label ("q/a/b.stl.stl" can settle on the
    short label "a/b.stl", which is also the literal ``rel`` that
    "a/b.stl" falls back to). A single dedup pass over the finished set
    catches the first shape but can produce the second: substituting one
    entry's ``rel`` in is not re-checked against the entries that already
    chose their labels. The loop below re-checks after every substitution
    and repeats until a pass makes no change, which terminates in at most
    ``len(models)`` passes because each entry that gets substituted lands on
    its own ``rel``, unique by construction, and is never substituted again.
    """
    segments = {m["rel"]: tuple(m["rel"].split("/")) for m in models}
    if not segments:
        return {}
    max_k = max(len(segs) for segs in segments.values())

    def suffix(segs, k):
        n = min(k, len(segs))
        tail = list(segs[-n:])
        tail[-1] = tail[-1].rsplit(".", 1)[0]
        return "/".join(tail)

    labels = {rel: suffix(segs, max_k) for rel, segs in segments.items()}
    unresolved = set(segments)
    for k in range(1, max_k + 1):
        if not unresolved:
            break
        candidates = {rel: suffix(segments[rel], k) for rel in unresolved}
        counts = Counter(candidates.values())
        for rel, candidate in candidates.items():
            if counts[candidate] == 1:
                labels[rel] = candidate
                unresolved.discard(rel)

    while True:
        counts = Counter(labels.values())
        collided = [rel for rel, label in labels.items() if counts[label] > 1]
        if not collided:
            break
        for rel in collided:
            labels[rel] = rel

    assert len(set(labels.values())) == len(labels), (
        "label computation produced a duplicate; this is a bug in "
        "_compute_labels, not a property of the input tree"
    )
    return labels


class ModelIndex:
    """Lookup table built from one scan, mapping the keys HTTP routes resolve
    model bytes through. Only files ``scan_models`` actually found are
    reachable this way; a request for anything else is a 404 regardless of
    whether a file of that name exists on disk, which is the point.

    ``manifest_models`` is the list a ``/manifest`` response advertises, and
    it is every scanned model: ``rel`` is unique by construction (a directory
    cannot hold two entries with one name), so every entry is always
    reachable by ``/model/<rel>``. ``file`` is only the bare filename, and
    once the scan descends, two entries at different depths can share one
    (``a/widget.stl`` and ``b/widget.stl``); an entry whose ``file`` is
    ambiguous this way is still listed and still fetchable by ``rel``, it is
    just absent from ``by_file``/``identity_of_file`` below, so the
    bare-filename alias route 404s for it rather than guessing which of the
    matching files to serve. ``models`` keeps the same list for callers that
    want the scan's contents rather than the advertised listing; the two
    diverge only if a future scan indexes files the viewer cannot fetch.
    """

    def __init__(self, serve_dir, models, truncated):
        self.serve_dir = serve_dir
        self.models = models
        self.truncated = truncated
        self._by_rel = {m["rel"]: Path(m["path"]) for m in models}
        # The identity the scan validated, kept out of the advertised listing:
        # inode numbers are noise in a public contract, and a route needs them
        # only to confirm that what it opened is what was checked.
        self._identity = {m["rel"]: (m["_dev"], m["_ino"]) for m in models}
        self.manifest_models = [
            {k: v for k, v in m.items() if not k.startswith("_")} for m in models
        ]

        # A bare filename identifies a model only when exactly one indexed
        # entry has it; an entry whose ``file`` some other entry also has is
        # left out of both maps below, rather than one of the pair
        # arbitrarily winning the dict key. Serving one of two files that
        # differ only in which directory they live under would be silently
        # serving the wrong model to whichever client asked by the ambiguous
        # name, with no way for that client to tell it had happened.
        file_counts = Counter(m["file"] for m in models)
        ambiguous = sorted(name for name, count in file_counts.items() if count > 1)
        if ambiguous:
            sys.stderr.write(
                "warning: %d model filename%s ambiguous across directories and "
                "unreachable by the bare-name alias: %s\n"
                % (len(ambiguous), "" if len(ambiguous) == 1 else "s", ", ".join(ambiguous))
            )
        self._by_file = {m["file"]: Path(m["path"]) for m in models if file_counts[m["file"]] == 1}
        self._rel_of_file = {m["file"]: m["rel"] for m in models if file_counts[m["file"]] == 1}

    def identity_of(self, rel):
        """Return the ``(st_dev, st_ino)`` the scan validated for ``rel``.

        A route resolves a path here and opens it a moment later, on another
        thread, and the scan's result is reused for up to a second. In that
        window the name can be relinked to a file anywhere on the same
        filesystem; a hardlink is not a link at the path level, so opening
        with O_NOFOLLOW does not notice. Comparing the opened descriptor
        against this pair does, because the swapped-in file is a different
        inode.
        """
        return self._identity.get(rel)

    def identity_of_file(self, name):
        """``identity_of`` for a bare filename, as the .stl alias resolves.

        None both when no indexed model has this filename and when more
        than one does; the caller cannot tell those two cases apart from
        this return value alone, which is deliberate, since either way there
        is no single file this name can mean.
        """
        rel = self._rel_of_file.get(name)
        return self._identity.get(rel) if rel is not None else None

    def by_rel(self, rel):
        """Resolve a POSIX-style relative path to an absolute Path, or None."""
        return self._by_rel.get(rel)

    def by_file(self, name):
        """Resolve a bare filename to an absolute Path, or None.

        None both when no indexed model has this filename and when more than
        one does: a recursive scan can index two files of the same name in
        different directories, so this alias must refuse rather than pick a
        winner between two files a client cannot tell apart by name alone.
        """
        return self._by_file.get(name)


def build_model_index(serve_dir):
    """Scan ``serve_dir`` and return a fresh ``ModelIndex`` of its current
    contents. The scan is synchronous filesystem work; callers on an
    asyncio event loop are responsible for running it off that loop and for
    deciding how often to call this versus reusing a previous result."""
    serve_dir = resolve_serve_dir(serve_dir)
    models, truncated = scan_models(serve_dir)
    return ModelIndex(serve_dir, models, truncated)


def _static_name_allowed(name, rel_parts):
    """Whether ``name`` (a filename directly under the path ``rel_parts``
    names) belongs in the static asset index; see STATIC_EXTENSIONS and
    STATIC_LICENSE_DIRNAME for what this allows and why."""
    if Path(name).suffix.lower() in STATIC_EXTENSIONS:
        return True
    return name == STATIC_LICENSE_FILENAME and STATIC_LICENSE_DIRNAME in rel_parts


def _static_content_type(rel):
    """Content type for a rel already confirmed present in a StaticIndex.

    Reuses CONTENT_TYPES, the same table the packaged viewer's own HTML and
    every model's bytes are served with: static/ is this package's own tree,
    installed alongside the code that reads it, not user-supplied input the
    way a served project's images/ subtree is, so there is no reason to
    narrow its content types the way ASSET_CONTENT_TYPES narrows /asset's.
    The one file with no extension to look up, the vendored LICENSE, is
    plain text.
    """
    suffix = Path(rel).suffix.lower()
    if suffix in CONTENT_TYPES:
        return CONTENT_TYPES[suffix]
    return "text/plain; charset=utf-8"


def scan_static(static_dir):
    """Scan the package's own ``static_dir`` for servable assets.

    Returns ``(entries, truncated)`` the same shape ``scan_models`` returns,
    minus the model-specific fields: each entry is ``{"rel", "path", "_dev",
    "_ino"}``. The walk shares scan_models's shape (an explicit stack over
    ``os.scandir``, dotfile and symlinked-directory exclusion, sorted
    traversal for reproducible results) because those rules are about
    walking a tree safely and deterministically, not about who controls its
    contents, and both scans need exactly the same care there.

    What differs is which files qualify, and why. ``scan_models`` defends a
    directory an outside party can write into at any time; static/ is fixed
    at install time and only as trustworthy as the Python environment
    running this code already is, so:

    * The extension allowlist (``_static_name_allowed``) is the qualifying
      test in place of ``scan_models``'s hardlink-and-extension check, since
      the risk here is not a hardlink smuggling in bytes from elsewhere but
      an unrelated file (an editor backup, a source map) sitting in the
      tree and becoming servable just by matching a route pattern.
    * Symlinks are still refused and directories are still not descended
      through one: a broken or redirected symlink in an installed package is
      a packaging bug, not a trust boundary, but refusing it costs nothing
      and a directory of static assets has no legitimate need for one.
    * There is no depth or directory-count cap to match MAX_SCAN_DEPTH or
      MAX_SCAN_DIRS: those defend against a served directory an attacker
      can pad with arbitrarily many real directories, and static/ is a
      fixed tree shipped by this package that no request can grow.
    * Hard link count is deliberately not checked; see the comment at that
      point in the loop below.
    """
    static_dir = resolve_serve_dir(static_dir)
    entries = []
    truncated = False
    stack = [(static_dir, ())]
    while stack:
        dirpath, rel_parts = stack.pop()
        try:
            children = sorted(os.scandir(dirpath), key=lambda e: e.name)
        except OSError:
            continue

        subdirs = []
        cap_hit = False
        for entry in children:
            if entry.name.startswith("."):
                continue
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISDIR(st.st_mode):
                subdirs.append(entry.name)
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            if not _static_name_allowed(entry.name, rel_parts):
                continue
            # Unlike scan_models, a second hard link is not refused here,
            # and that asymmetry is deliberate. uv and pip both install a
            # package's files by hardlinking them out of a local wheel
            # cache rather than copying, so a legitimately installed
            # three.module.js routinely has more than one link, and
            # refusing it would make a normal install unservable. The
            # served project directory the model scan defends is untrusted
            # input a reviewer did not write; this package's own static/
            # tree is not, because anything with write access to
            # site-packages already controls this process outright, hard
            # links or not. The symlink refusal and the dotfile exclusion
            # above still apply to this tree exactly as they do to a served
            # project's.
            if len(entries) >= MAX_STATIC_FILES:
                truncated = True
                cap_hit = True
                break
            fpath = dirpath / entry.name
            entries.append(
                {
                    "rel": "/".join(rel_parts + (entry.name,)),
                    "path": fpath,
                    "_dev": st.st_dev,
                    "_ino": st.st_ino,
                }
            )
        if cap_hit:
            sys.stderr.write(
                "warning: static asset index stopped at the %d file cap "
                "(MAX_STATIC_FILES); an installed static/ tree should never "
                "be this large\n" % MAX_STATIC_FILES
            )
            break
        for name in sorted(subdirs, reverse=True):
            stack.append((dirpath / name, rel_parts + (name,)))

    entries.sort(key=lambda e: e["rel"])
    return entries, truncated


class StaticIndex:
    """Lookup table over the package's own ``static/`` directory, built the
    same way ``ModelIndex`` is built over a served project directory: every
    route that serves a packaged asset resolves through this rather than
    joining a request path onto disk, so a file present in the tree but not
    matching ``scan_static``'s rules (wrong extension, a symlink, sitting
    under a dotdir) simply is not a key here and is therefore unreachable,
    the same guarantee ``ModelIndex`` gives for models.
    """

    def __init__(self, static_dir, entries, truncated):
        self.static_dir = static_dir
        self.truncated = truncated
        self._by_rel = {e["rel"]: Path(e["path"]) for e in entries}
        self._identity = {e["rel"]: (e["_dev"], e["_ino"]) for e in entries}

    def by_rel(self, rel):
        """Resolve a POSIX-style relative path to an absolute Path, or None."""
        return self._by_rel.get(rel)

    def identity_of(self, rel):
        """Return the ``(st_dev, st_ino)`` the scan validated for ``rel``,
        for the same reopen-time check ``ModelIndex.identity_of`` supports;
        see that method for why a scan result and the later open can
        disagree even without anything hostile involved (an editor's
        write-new-file-then-rename-over-old edit is indistinguishable, at
        the moment of the open, from a name relinked to something else)."""
        return self._identity.get(rel)

    @staticmethod
    def content_type_of(rel):
        """Content type to serve ``rel`` with; see ``_static_content_type``.

        A static method rather than one reading ``self``: the answer depends
        only on ``rel`` itself, not on any particular scan, so a route can
        call it before or without ever fetching an index instance, and two
        different ``StaticIndex`` instances always agree on it for the same
        ``rel``.
        """
        return _static_content_type(rel)


def build_static_index(static_dir):
    """Scan ``static_dir`` and return a fresh ``StaticIndex`` of its current
    contents. Synchronous filesystem work, same executor-offload contract as
    ``build_model_index``."""
    static_dir = resolve_serve_dir(static_dir)
    entries, truncated = scan_static(static_dir)
    return StaticIndex(static_dir, entries, truncated)
