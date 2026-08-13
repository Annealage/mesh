"""Served-directory resolution, the model scan, and path safety.

This module has no HTTP awareness: it knows about a served directory and the
files under it, not requests or responses. ``app.py`` and
``http/routes_viewer.py`` turn what this module produces into HTTP
responses; this module alone decides which files on disk a client is allowed
to reach, which keeps the one piece of security-relevant logic in one place.

Files this module reads:
    mesh-callouts.json   agent-authored callouts, read whole by
                         ``read_fixed_file``

Files this module opens for the caller to write, without writing itself:
    mesh-comments.log    opened once by ``open_fixed_file_for_append``
    images/*.png         created by ``create_image_file`` for a captured view

Files this module only locates, so writer and resolver agree where they live:
    mesh-comments.json   human submissions, replaced atomically by the caller
"""

import os
import re
import secrets
import stat
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

COMMENTS_JSON_NAME = "mesh-comments.json"
COMMENTS_LOG_NAME = "mesh-comments.log"
CALLOUTS_JSON_NAME = "mesh-callouts.json"
IMAGES_DIRNAME = "images"

# Minimal extension -> content-type map (stdlib mimetypes misses .stl/.3mf).
# Used for the packaged viewer, model bytes and callouts.json, none of
# which are files an outside party can place into the served directory
# under a name of their choosing; a served directory's images/ subtree is,
# so /asset uses ASSET_CONTENT_TYPES below instead of this map.
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

# Content types the /asset route will report. Deliberately narrower than
# CONTENT_TYPES: images/ can contain whatever a reviewed bundle happened to
# ship, so a file saved with a ".html" or ".svg" extension must never be
# labelled as active content on this server's own origin, from where a
# script could read /manifest, every indexed model, and POST /submit. Any
# extension not listed here is served as application/octet-stream rather
# than falling back to a guess; PNG, JPEG and WEBP are the three formats
# sniff_image ever names, and are the only ones a reviewer's own photos,
# screenshots and uploads use.
ASSET_CONTENT_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
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

# Cap on a fixed-name exchange file read whole into memory. A callout list a
# person or an agent wrote is kilobytes; this bounds what a file substituted
# at that name can cost.
_MAX_FIXED_FILE_BYTES = 4 * 1024 * 1024

# Flags every open of a fixed-name exchange file carries, so the descriptor
# cannot be redirected or made to block by whatever sits at that name.
#
# O_NOFOLLOW refuses a symlink at the final component. It says nothing about
# a FIFO, and opening one blocks in the kernel until the other end appears:
# for reading until a writer arrives, which for a directory nobody is writing
# to is never. These opens run in an executor thread, so each one would
# consume a thread from a small pool permanently.
#
# O_NONBLOCK stops that, but the two directions behave differently and only
# one of them fails: O_WRONLY|O_NONBLOCK on a FIFO with no reader returns
# ENXIO, while O_RDONLY|O_NONBLOCK succeeds immediately. What refuses the read
# case is the S_ISREG check on the resulting descriptor; O_NONBLOCK's job
# there is only to make sure that check is reached at all.
#
# Neither flag sees a hardlink, because a hardlink is not a link at the path
# level; the fstat check on the resulting descriptor covers that.
OPEN_GUARD_FLAGS = getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)

# What a file this package creates under ``images/`` may be called. A single
# component of the ordinary filename characters, not starting with a dot, and
# ending in one of the extensions the /asset route already serves: a name this
# pattern accepts is one that route can hand back, so nothing is ever written
# that cannot then be looked at. The pattern is the whole containment check for
# a model-supplied name, which is why it is a whitelist and not a blacklist of
# ``..`` and ``/``.
_IMAGE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}\.(png|jpg|jpeg|webp)$",
                            re.IGNORECASE)

# Mode for an image this package creates. Matches the record files rather than
# mkstemp's 0600, because these are meant to be committed and looked at.
_IMAGE_FILE_MODE = 0o644

# Cap on the declared size of an image this package accepts into images/,
# whether uploaded through the chat route or read back off disk for an
# inline turn block. Equal to review_tools.MAX_SNAPSHOT_BYTES, since both
# bound one image landing in images/ as git-tracked evidence, and no larger
# than app.MAX_REQUEST_BODY, which bounds a whole request body for every
# route in the process. The two constants are equal today, which is why an
# oversized upload's Content-Length is answered by microdot's own 413
# before a route ever consults this one.
MAX_IMAGE_BYTES = 8 * 1024 * 1024

# Cap on one image copied *into a turn* as an inline base64 block, which is a
# different and much tighter question than what may be stored under images/.
# Two ceilings bind it, both outside this process: the Messages API refuses an
# inline image whose base64 payload runs past roughly 5 MB, and the agent SDK's
# subprocess transport refuses to read a line longer than its own buffer, which
# session/sdk.py sizes from this constant. Base64 inflates by four thirds, so
# 3.5 MiB decoded leaves room under both for the JSON around it.
#
# A file above this is still stored, still served by /asset and still named to
# the model in the turn's text; only the inline copy is withheld, because a
# photograph a human deliberately attached is evidence worth keeping at full
# resolution, and the model can read it by path with its own tools. Downscaling
# instead would mean an image library, and two runtime dependencies is a
# property of this package worth more than the convenience.
MAX_INLINE_IMAGE_BYTES = 3584 * 1024

# Magic-byte prefixes sniff_image matches at the start of a file's bytes,
# never against a client-supplied Content-Type or filename: an image's
# format is a property of its bytes, and a served origin that trusted a
# label disagreeing with them could be made to serve a script as a picture.
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_JPEG_MAGIC = b"\xff\xd8\xff"

# The shortest head sniff_image will match against, and equally the most it
# ever looks at, so a caller streaming a body knows exactly how few bytes it
# must buffer before a verdict is available. WEBP's own signature spans the
# RIFF chunk header (bytes 0-3) and the four-byte format code at bytes 8-11,
# so nothing shorter can ever distinguish WEBP from some other RIFF-contained
# format; PNG and JPEG have shorter signatures of their own, but the same
# floor applies to all three, so one rule governs every format this function
# recognises.
SNIFF_MIN_BYTES = 12


def sniff_image(head):
    """Return ``(media_type, suffix)`` for the image ``head`` begins, else None.

    ``head`` is the first bytes of a file's content. Fewer than
    ``SNIFF_MIN_BYTES`` of them is treated as unrecognised rather than
    matched against a shorter prefix, so a read that stopped early is
    refused the same way a genuinely different format is, never mistaken
    for one of the three this function knows by having matched only part
    of its signature. Bytes past that floor are never examined, so a caller
    has read enough as soon as it has that many.
    """
    if len(head) < SNIFF_MIN_BYTES:
        return None
    if head.startswith(_PNG_MAGIC):
        return "image/png", ".png"
    if head.startswith(_JPEG_MAGIC):
        return "image/jpeg", ".jpg"
    if head[0:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp", ".webp"
    return None


def resolve_serve_dir(path):
    """Resolve ``path`` to an absolute, symlink-free directory path."""
    return Path(path).resolve()


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
                    "file outside the served directory\n" % (
                        dirpath / entry.name, st.st_nlink))
                continue
            if len(models) >= MAX_INDEXED_FILES:
                truncated = True
                cap_hit = True
                break
            fpath = dirpath / entry.name
            models.append({
                "name": Path(entry.name).stem,
                "file": entry.name,
                "path": str(fpath),
                "rel": "/".join(rel_parts + (entry.name,)),
                # Not advertised; the index moves these into a side table so
                # a route can require the file it opens to be the one
                # checked here.
                "_dev": st.st_dev,
                "_ino": st.st_ino,
            })
        if cap_hit:
            break
        for name in sorted(subdirs, reverse=True):
            child_depth = depth + 1
            if child_depth > MAX_SCAN_DEPTH:
                truncated = True
                sys.stderr.write(
                    "warning: not descending into %s: more than MAX_SCAN_DEPTH "
                    "(%d) directories deep\n" % (dirpath / name, MAX_SCAN_DEPTH))
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
        "_compute_labels, not a property of the input tree")
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
            {k: v for k, v in m.items() if not k.startswith("_")} for m in models]

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
                "unreachable by the bare-name alias: %s\n" % (
                    len(ambiguous), "" if len(ambiguous) == 1 else "s",
                    ", ".join(ambiguous)))
        self._by_file = {
            m["file"]: Path(m["path"]) for m in models if file_counts[m["file"]] == 1}
        self._rel_of_file = {
            m["file"]: m["rel"] for m in models if file_counts[m["file"]] == 1}

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


def file_validator(st):
    """An HTTP entity tag for the file ``st`` describes, as a weak validator.

    Derived from the stat's identity and its size and modification time, not
    from the file's content: hashing a megabyte of vendored JavaScript on
    every scan would cost more than the transfer it saves. The tag is
    therefore weak, and is labelled ``W/`` accordingly, which is honest about
    what it can prove and is all a conditional whole-document request needs.
    Range requests, which cannot use a weak validator, are not served.

    Including ``st_dev`` and ``st_ino`` is what makes the tag identity-aware:
    a name relinked to a different file produces a different tag even if the
    replacement's size and timestamp match, so a caller comparing a client's
    tag against a freshly stat'd file cannot be talked into affirming a
    cached copy of bytes that name no longer refers to.
    """
    return 'W/"%x-%x-%x-%x"' % (st.st_dev, st.st_ino, st.st_mtime_ns, st.st_size)


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
            entries.append({
                "rel": "/".join(rel_parts + (entry.name,)),
                "path": fpath,
                "_dev": st.st_dev,
                "_ino": st.st_ino,
            })
        if cap_hit:
            sys.stderr.write(
                "warning: static asset index stopped at the %d file cap "
                "(MAX_STATIC_FILES); an installed static/ tree should never "
                "be this large\n" % MAX_STATIC_FILES)
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


def resolve_asset(serve_dir, rel):
    """Resolve ``rel`` to a file under ``serve_dir``/images, or None.

    Returns None if the images/ subdirectory does not exist, if it resolves
    outside serve_dir, or if ``rel`` is empty or escapes images/ (including
    via a symlink that resolves outside it), or if the resolved target is
    not a file. images/ itself may be a symlink, but its resolved target
    must still be inside serve_dir: a symlink named images/ pointing
    anywhere else (a reviewed bundle can carry one, e.g. in a zip or a git
    clone) would otherwise turn every /asset request into a read of
    anything under that other location that the server process can open,
    which is the exact disclosure this route exists to prevent.
    """
    serve_dir = resolve_serve_dir(serve_dir)
    images_dir = serve_dir / IMAGES_DIRNAME
    # images/ must be a real directory. A symlink here cannot be made safe
    # by checking where it points, because ``is_relative_to`` is satisfied
    # by serve_dir itself: an "images -> ." link passes containment and then
    # becomes the base every /asset request is joined against, which turns
    # this route back into the serve-anything fallback it replaces. Pointing
    # it at a subdirectory is no better, since that subdirectory's contents
    # were never indexed and so were never meant to be reachable.
    if images_dir.is_symlink():
        return None
    try:
        resolved_images_dir = images_dir.resolve()
    except OSError:
        return None
    if resolved_images_dir == serve_dir:
        return None
    if not resolved_images_dir.is_relative_to(serve_dir):
        return None
    if not resolved_images_dir.is_dir():
        return None
    return safe_join(resolved_images_dir, rel)


def safe_fixed_file(serve_dir, name):
    """Return ``serve_dir``/``name`` if it is safe to read or write, else None.

    The three exchange file names are fixed by this package, not supplied by
    a client, but their directory entries are not: a reviewed bundle, a zip
    or a git clone can carry any of them as something other than the plain
    file this code expects, and following it would read or write elsewhere.

    An absent entry is allowed, since the writer creates it. An entry that
    exists must satisfy all of:

    * a regular file, so a FIFO cannot be opened. Opening a FIFO for writing
      blocks until a reader appears, and that open happens in an executor
      thread, so each attempt would consume one thread from a small pool and
      never return it.
    * exactly one hard link. A hardlink is not a link at the path level, so
      neither ``is_symlink`` nor ``O_NOFOLLOW`` sees it, yet its contents are
      a file elsewhere on the same filesystem.
    * not a symlink, and sitting directly in serve_dir.

    This is a check, and any caller that then opens the path by name has a
    window between the two. Readers should use ``read_fixed_file`` and
    writers should pass ``O_NOFOLLOW``, so the guarantee is re-established
    against the file descriptor actually in use.
    """
    serve_dir = resolve_serve_dir(serve_dir)
    target = serve_dir / name
    if target.parent != serve_dir:
        return None
    try:
        st = os.lstat(target)
    except FileNotFoundError:
        return target
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode):
        return None
    # Zero means the name was unlinked between the lookup and now, which this
    # process does to itself on every atomic replace, and on some filesystems
    # the lookup lands on the outgoing inode. Treat it as absent, which is
    # what it is, rather than as evidence of tampering.
    if st.st_nlink > 1:
        return None
    return target


def open_fixed_file_for_append(serve_dir, name, mode):
    """Open a fixed-name file for appending and return the descriptor, or None.

    The descriptor is meant to be held for the process's lifetime rather than
    reopened per write, which is what makes this safe. Resolving the name on
    every write cannot be made safe: an attacker with write access to the
    served directory hardlinks a file from outside it to this name, waits for
    the open to land on that inode, then unlinks their own link, so by the
    time any check runs the link count is back to one and every later append
    goes into a file outside the tree. Opening once removes the window from
    every write after the first.

    Validation at that single open requires all of:

    * O_NOFOLLOW, so a symlink at the name is refused outright.
    * O_NONBLOCK, so a FIFO fails with ENXIO instead of blocking in the
      kernel until a reader appears, which for an executor thread means
      never.
    * a regular file with exactly one link, so a hardlink to somewhere else
      is refused while both names exist.
    * the same inode still present at the name afterwards, which closes the
      unlink race above: if the attacker removed their link the second lookup
      fails, and if they replaced the entry it names a different inode.

    Once past that, the file is what it appeared to be. Later renaming or
    deleting it does not redirect the appends, which continue into the inode
    originally opened; that is how any long-running process treats its log.
    """
    target = safe_fixed_file(serve_dir, name)
    if target is None:
        return None
    try:
        fd = os.open(
            target,
            os.O_WRONLY | os.O_CREAT | os.O_APPEND | OPEN_GUARD_FLAGS,
            mode)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            os.close(fd)
            return None
        entry = os.stat(target, follow_symlinks=False)
        if (entry.st_dev, entry.st_ino) != (st.st_dev, st.st_ino):
            os.close(fd)
            return None
    except OSError:
        os.close(fd)
        return None
    return fd


def read_fixed_file(serve_dir, name):
    """Read one of the fixed-name exchange files, or return None.

    Opens with ``O_NOFOLLOW`` and re-checks the open descriptor with
    ``fstat`` rather than trusting the earlier path check, so a symlink,
    hardlink or FIFO substituted between the check and the open cannot
    redirect the read. That race is winnable in practice: the read happens
    in an executor thread, which widens the window considerably.

    These files hold a callout list a person or an agent wrote by hand, so
    they are read whole rather than streamed. ``_MAX_FIXED_FILE_BYTES`` caps
    what a substituted file can cost.
    """
    target = safe_fixed_file(serve_dir, name)
    if target is None:
        return None
    try:
        fd = os.open(target, os.O_RDONLY | OPEN_GUARD_FLAGS)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            return None
        # The same recheck the append open performs, and for the same reason:
        # a single link count does not establish that the inode opened is the
        # entry named in the served directory. An attacker hardlinks a file
        # from outside onto this name, the open lands on that inode, and they
        # then remove their own link, so the count is back to one while the
        # descriptor points outside the tree. Requiring the name to still
        # resolve to this inode refuses that, because the removed link makes
        # the lookup fail and a replaced entry names something else.
        entry = os.stat(target, follow_symlinks=False)
        if (entry.st_dev, entry.st_ino) != (st.st_dev, st.st_ino):
            return None
        if st.st_size > _MAX_FIXED_FILE_BYTES:
            sys.stderr.write(
                "warning: %s is %d bytes, over the %d byte cap; treating it as "
                "having no callouts\n" % (target, st.st_size, _MAX_FIXED_FILE_BYTES))
            return None
        return os.read(fd, _MAX_FIXED_FILE_BYTES)
    except OSError:
        return None
    finally:
        os.close(fd)


def atomic_replace(target, data, default_mode=0o644):
    """Write ``data`` to ``target`` so a reader never sees a partial file.

    The bytes go to a temporary file in ``target``'s own directory and are
    moved into place with ``os.replace``, which is atomic on POSIX and on
    Windows. Two writers therefore leave one complete file rather than one's
    bytes overlaid on the other's, and a reader either sees the old contents or
    the new ones. That matters for every file in the served directory that
    something else polls: the callouts watcher samples a digest several times a
    second, and the viewer fetches the file the moment it changes.

    ``os.replace`` acts on the directory entry, so it writes through neither a
    symlink nor a hardlink sitting at the destination; ``safe_fixed_file``'s
    checks are about what a *reader* would follow and what an appending writer
    would land on, not about this path.

    The temporary name is dot-prefixed so the model scan skips one left behind
    by a crash. An existing file's mode is carried across, so a deliberate
    choice by the operator survives a rewrite, and ``default_mode`` applies
    only to a file this call creates: ``mkstemp`` makes 0600 and ``os.replace``
    would otherwise silently narrow the file on every write.

    Raises ``OSError``; the caller decides what a failed write means.
    """
    target = Path(target)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(target.parent), prefix=".%s." % target.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.chmod(tmp_name, stat.S_IMODE(os.stat(target, follow_symlinks=False).st_mode))
        except OSError:
            os.chmod(tmp_name, default_mode)
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return target


def create_image_file(serve_dir, name):
    """Create ``serve_dir``/images/``name`` for writing and return its descriptor.

    Returns None when the name is not one this package will write or the
    ``images`` entry is not something safe to write into, and raises
    ``FileExistsError`` when that exact name is already taken, which the caller
    is expected to answer by choosing another one rather than by overwriting:
    every image under ``images/`` is git-tracked evidence of what a part looked
    like at some moment, and silently replacing one loses that.

    ``name`` is a single path component matching ``_IMAGE_NAME_RE``, so it
    cannot traverse, cannot be hidden, and cannot arrive with a suffix this
    package does not serve. That matters because the name may come from the
    model: a snapshot tool takes one so the file is findable afterwards, and a
    model-supplied string is exactly the input a containment check exists for.

    ``images/`` is created if absent, and refused if it is a symlink. Refusing
    the link matches ``resolve_asset``'s rule and is not paranoia about reading:
    a link named ``images`` makes this a writer into whatever it points at,
    which is a way to have this process create files anywhere its user can.
    The open carries ``O_EXCL`` and ``O_NOFOLLOW`` so the descriptor is a file
    this call created, at this name, rather than whatever appeared there while
    the checks above were running.
    """
    serve_dir = resolve_serve_dir(serve_dir)
    if not _IMAGE_NAME_RE.match(name or ""):
        return None
    images_dir = serve_dir / IMAGES_DIRNAME
    if images_dir.is_symlink():
        return None
    try:
        images_dir.mkdir(exist_ok=True)
    except OSError:
        return None
    if not images_dir.is_dir():
        return None
    target = images_dir / name
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(target, flags, _IMAGE_FILE_MODE)
    return fd, target


# How many names create_unique_image_file tries before raising FileExistsError.
# Each attempt draws a fresh random suffix, so two attempts collide only by
# chance; twenty is far more headroom than that chance ever needs while still
# bounding the loop.
UNIQUE_IMAGE_NAME_ATTEMPTS = 20


def create_unique_image_file(serve_dir, slug, suffix):
    """Create a fresh, server-named file under ``serve_dir``/images/ for writing.

    Returns whatever ``create_image_file`` returns for the name this chooses:
    ``(fd, target)``, or None when the ``images`` entry is not something safe
    to write into, in which case no attempt at a second name is made, since a
    symlinked ``images/`` is not fixed by trying another name. Raises
    ``FileExistsError`` if every attempt lands on a name already taken, which
    takes the same slug, the same second and the same random suffix landing
    together, on every one of ``UNIQUE_IMAGE_NAME_ATTEMPTS`` tries.

    The name is ``<slug>-<%Y%m%d-%H%M%S>-<8 hex chars><suffix>``, built here
    and nowhere else. ``slug`` and ``suffix`` are the only inputs the name is
    built from, and neither is a client-supplied filename: a caller that wants
    a client's own upload named this way passes the slug its own query
    parameter validated and the suffix ``sniff_image`` returned for the
    bytes actually received, not anything the client typed as a name.
    """
    stamp = time.strftime("%Y%m%d-%H%M%S")
    name = None
    for _attempt in range(UNIQUE_IMAGE_NAME_ATTEMPTS):
        name = "%s-%s-%s%s" % (slug, stamp, secrets.token_hex(4), suffix)
        try:
            created = create_image_file(serve_dir, name)
        except FileExistsError:
            continue
        return created
    raise FileExistsError(name)


def safe_join(base, rel):
    """Resolve a URL-supplied relative path ``rel`` to a file under ``base``.

    ``base`` must already be an absolute, resolved directory. Returns
    ``(path, (st_dev, st_ino))`` or None. None if ``rel`` is empty, contains a
    NUL byte, has a path component starting with a dot (matching the model
    scan's exclusion, so ``.git`` and hidden files stay unreachable), or the
    resolved target, following any symlinks, is not inside ``base``, is not a
    regular file, or carries more than one hard link.

    The identity is returned so the caller can require the descriptor it
    later opens to be this same inode: resolution and open are separate
    operations, and the name can be relinked in between. This is the one
    general-purpose path-safety check in the package; model routes go through
    ``ModelIndex``, which pins identity from the scan instead.
    """
    if not rel or "\x00" in rel:
        return None
    rel = rel.lstrip("/")
    if not rel:
        return None
    if any(part.startswith(".") for part in Path(rel).parts):
        return None
    target = base / rel
    try:
        resolved = target.resolve()
    except OSError:
        return None
    if not resolved.is_relative_to(base):
        return None
    try:
        st = os.stat(resolved)
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode):
        return None
    # A second link means the bytes may belong to a file outside ``base``,
    # which resolving the name cannot reveal, since a hardlink is not a link
    # at the path level.
    if st.st_nlink != 1:
        return None
    return resolved, (st.st_dev, st.st_ino)
