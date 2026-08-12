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

Files this module only locates, so writer and resolver agree where they live:
    mesh-comments.json   human submissions, replaced atomically by the caller
"""

import os
import stat
import sys
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
# than falling back to a guess; the actual image types a reviewer's photos
# and screenshots use are the ones listed.
ASSET_CONTENT_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}

# The manifest scan indexes .stl files only, and only at the top level of the
# served directory. See scan_models for why it does not descend.
MODEL_EXTENSIONS = {".stl"}

# Cap on the number of files one scan will index. Past this many matching
# files, the scan stops early: files beyond the cap are simply absent from
# the manifest (and therefore unreachable via /model or the .stl alias)
# until the directory is pruned below the cap or the cap is raised. This
# bounds one scan's cost on a served directory of unbounded size; it is not
# a security control.
MAX_INDEXED_FILES = 5000

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
    """Scan the top level of ``serve_dir`` for indexable model files.

    Returns ``(models, truncated)``. ``models`` is a list of dicts sorted by
    ``rel``, each with:

        name        file stem, e.g. "widget"
        file        bare filename, e.g. "widget.stl"
        path        absolute filesystem path, as a string
        rel         POSIX-style path relative to serve_dir, which at this
                    depth equals ``file``; it is the key /model/<rel>
                    resolves through

    The scan does not descend into subdirectories. The packaged viewer keys
    its mesh table by ``name`` and fetches by ``file`` (``viewer.html`` around
    lines 225 and 235), so two models sharing either field collide in the
    browser: one overwrites the other's mesh, or both fetch the same bytes.
    A flat scan cannot produce that collision, because a directory cannot
    hold two entries with one name. Indexing subdirectories requires the
    viewer to fetch by ``rel`` instead, which is a change to that file, and
    ``rel`` is already carried here so it can be made without a contract
    change.

    Dotfiles are excluded, which covers a stray ``.mesh-comments-*.tmp``.
    An entry must be a regular file with exactly one hard link, and a symlink
    is refused rather than followed; see the comments in the body for why
    resolving a target cannot be made safe here.

    ``truncated`` is True once MAX_INDEXED_FILES matching files have been
    found; see that constant for what happens to files beyond the cap.
    """
    serve_dir = resolve_serve_dir(serve_dir)
    models = []
    truncated = False
    try:
        entries = sorted(p.name for p in serve_dir.iterdir())
    except OSError:
        return models, truncated
    for fname in entries:
        if fname.startswith("."):
            continue
        if Path(fname).suffix.lower() not in MODEL_EXTENSIONS:
            continue
        fpath = serve_dir / fname
        try:
            st = os.lstat(fpath)
        except OSError:
            continue
        # One lstat decides everything, and nothing is resolved. A symlink is
        # refused outright rather than having its target validated: validating
        # a target means resolving a path, resolving is several lookups, and an
        # attacker who can write to this directory can change the entry
        # between them, so a link pointing outside the tree can be made to
        # pass a check applied to what the name pointed at a moment earlier.
        # A directory of models to review has no need of symlinks, so the
        # simple rule is also the safe one.
        if not stat.S_ISREG(st.st_mode):
            continue
        # A second link means the bytes may be a file from anywhere on the
        # filesystem, which the name cannot reveal, so serving them would
        # publish a file outside the served directory.
        if st.st_nlink != 1:
            sys.stderr.write(
                "warning: skipping %s: %d hard links, so its contents may be a "
                "file outside the served directory\n" % (fpath, st.st_nlink))
            continue
        if len(models) >= MAX_INDEXED_FILES:
            truncated = True
            break
        models.append({
            "name": fpath.stem,
            "file": fpath.name,
            "path": str(fpath),
            "rel": fpath.name,
            # Not advertised; the index moves these into a side table so a
            # route can require the file it opens to be the one checked here.
            "_dev": st.st_dev,
            "_ino": st.st_ino,
        })
    return models, truncated


class ModelIndex:
    """Lookup table built from one scan, mapping the keys HTTP routes resolve
    model bytes through. Only files ``scan_models`` actually found are
    reachable this way; a request for anything else is a 404 regardless of
    whether a file of that name exists on disk, which is the point.

    ``manifest_models`` is the list a ``/manifest`` response advertises, and
    with a flat scan it is every scanned model: ``file`` is unique within one
    directory, so each entry is reachable both by the bare-filename alias the
    shipped viewer uses (``loader.load(p.file)``) and by ``/model/<rel>``.
    ``models`` keeps the same list for callers that want the scan's contents
    rather than the advertised listing; the two diverge only if a future
    scan indexes files the viewer cannot fetch.
    """

    def __init__(self, serve_dir, models, truncated):
        self.serve_dir = serve_dir
        self.models = models
        self.truncated = truncated
        self._by_rel = {m["rel"]: Path(m["path"]) for m in models}
        # The scan is flat, so ``file`` is unique across models and every
        # entry is both listed and reachable by bare filename.
        # The identity the scan validated, kept out of the advertised listing:
        # inode numbers are noise in a public contract, and a route needs them
        # only to confirm that what it opened is what was checked.
        self._identity = {m["rel"]: (m["_dev"], m["_ino"]) for m in models}
        self.manifest_models = [
            {k: v for k, v in m.items() if not k.startswith("_")} for m in models]
        self._by_file = {m["file"]: Path(m["path"]) for m in models}
        self._rel_of_file = {m["file"]: m["rel"] for m in models}

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
        """``identity_of`` for a bare filename, as the .stl alias resolves."""
        rel = self._rel_of_file.get(name)
        return self._identity.get(rel) if rel is not None else None

    def by_rel(self, rel):
        """Resolve a POSIX-style relative path to an absolute Path, or None."""
        return self._by_rel.get(rel)

    def by_file(self, name):
        """Resolve a bare filename to an absolute Path, or None if no
        indexed file has that name. The scan is flat, so a name identifies at
        most one model."""
        return self._by_file.get(name)


def build_model_index(serve_dir):
    """Scan ``serve_dir`` and return a fresh ``ModelIndex`` of its current
    contents. The scan is synchronous filesystem work; callers on an
    asyncio event loop are responsible for running it off that loop and for
    deciding how often to call this versus reusing a previous result."""
    serve_dir = resolve_serve_dir(serve_dir)
    models, truncated = scan_models(serve_dir)
    return ModelIndex(serve_dir, models, truncated)


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
