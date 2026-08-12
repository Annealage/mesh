"""Tests for the models watcher: the push that makes a regenerated part visible.

This is what turns the tool from a review surface into a modelling loop. An
agent that edits a CAD source and regenerates an STL has changed the thing being
discussed, and until this existed the viewer went on showing the previous
geometry until the human reopened the page, which is the moment they are least
likely to suspect the picture is stale.

Every test drives ``tick`` with the time it wants to pretend it is, rather than
starting ``run`` and sleeping, so the deferral rule is asserted rather than
approximated.

The change signal is size and modification time, with a content digest computed
only when those cannot settle the question: an STL is routinely megabytes and
this is sampled several times a second, so hashing everything every tick would
be wasteful, and hashing nothing would miss the case
``test_a_same_size_same_mtime_rewrite_is_still_detected`` covers, which is not
hypothetical. The readiness test is the project's own STL reader, so a
half-written model is waited for rather than announced.
"""

import os
import struct
import time

import pytest

from annealage_mesh.app import ModelsWatcher, _models_signature
from annealage_mesh.session.events import EventLog

pytestmark = pytest.mark.asyncio


class _RecordingRegistry:
    def __init__(self):
        self.frames = []

    async def broadcast(self, frame):
        self.frames.append(frame)


def _watcher(serve_dir, max_defer=5.0):
    registry = _RecordingRegistry()
    return ModelsWatcher(serve_dir, registry, EventLog(), max_defer=max_defer), registry


def _stl_bytes(triangles=2, z=0.0):
    """A structurally complete binary STL with ``triangles`` records."""
    out = [b"\0" * 80, struct.pack("<I", triangles)]
    for i in range(triangles):
        out.append(struct.pack(
            "<12fH",
            0.0, 0.0, 1.0,
            0.0, 0.0, z,
            1.0 + i, 0.0, z,
            0.0, 1.0, z,
            0))
    return b"".join(out)


def _write_model(serve_dir, name, triangles=2, z=0.0):
    (serve_dir / name).write_bytes(_stl_bytes(triangles, z))


async def _prime(watcher):
    """Run the first tick, which records and announces nothing."""
    assert await watcher.tick(0.0) is False


# -- what counts as a change ------------------------------------------------


async def test_the_first_tick_announces_nothing(tmp_path):
    """The state it starts in is what the page's own fetch on load covers, so
    announcing it would tell the browser to refetch what it just fetched."""
    _write_model(tmp_path, "part.stl")
    watcher, registry = _watcher(tmp_path)
    await _prime(watcher)
    assert registry.frames == []


async def test_an_unchanged_directory_announces_nothing(tmp_path):
    _write_model(tmp_path, "part.stl")
    watcher, registry = _watcher(tmp_path)
    await _prime(watcher)
    assert await watcher.tick(1.0) is False
    assert registry.frames == []


async def test_a_new_model_is_announced(tmp_path):
    """The agent generating a part that did not exist before."""
    _write_model(tmp_path, "part.stl")
    watcher, registry = _watcher(tmp_path)
    await _prime(watcher)

    _write_model(tmp_path, "clip.stl")
    assert await watcher.tick(1.0) is True
    assert registry.frames[-1]["event"]["kind"] == "models_changed"


async def test_a_regenerated_model_is_announced(tmp_path):
    """The central case: same filename, different geometry."""
    _write_model(tmp_path, "part.stl", triangles=2)
    watcher, registry = _watcher(tmp_path)
    await _prime(watcher)

    _write_model(tmp_path, "part.stl", triangles=8)
    assert await watcher.tick(1.0) is True


async def test_a_removed_model_is_announced(tmp_path):
    _write_model(tmp_path, "part.stl")
    _write_model(tmp_path, "clip.stl")
    watcher, registry = _watcher(tmp_path)
    await _prime(watcher)

    (tmp_path / "clip.stl").unlink()
    assert await watcher.tick(1.0) is True


async def test_one_change_is_announced_once(tmp_path):
    """The signature is updated when the event goes out, so a change does not
    re-announce itself on every subsequent tick."""
    _write_model(tmp_path, "part.stl")
    watcher, registry = _watcher(tmp_path)
    await _prime(watcher)

    _write_model(tmp_path, "part.stl", triangles=8)
    assert await watcher.tick(1.0) is True
    assert await watcher.tick(2.0) is False
    assert len(registry.frames) == 1


async def test_a_non_model_file_is_not_a_change(tmp_path):
    """The signature comes from the same scan the manifest uses, so anything the
    manifest ignores is ignored here without being restated."""
    _write_model(tmp_path, "part.stl")
    watcher, registry = _watcher(tmp_path)
    await _prime(watcher)

    (tmp_path / "notes.md").write_text("not a model")
    (tmp_path / "mesh-callouts.json").write_text("{}")
    assert await watcher.tick(1.0) is False


async def test_a_dotdir_model_is_not_a_change(tmp_path):
    (tmp_path / "part.stl").write_bytes(_stl_bytes())
    watcher, _registry = _watcher(tmp_path)
    await _prime(watcher)

    hidden = tmp_path / ".mesh"
    hidden.mkdir()
    _write_model(hidden, "cached.stl")
    assert await watcher.tick(1.0) is False


# -- half-written models ----------------------------------------------------


async def test_a_truncated_model_is_waited_for_not_announced(tmp_path):
    """A model large enough to be caught mid-write would otherwise be announced
    at the moment it is unreadable, and the viewer would report a load failure
    for a part that is about to be perfectly fine."""
    _write_model(tmp_path, "part.stl")
    watcher, registry = _watcher(tmp_path)
    await _prime(watcher)

    # A header claiming 40 triangles with only 2 present.
    (tmp_path / "part.stl").write_bytes(
        b"\0" * 80 + struct.pack("<I", 40) + _stl_bytes(2)[84:])
    assert await watcher.tick(1.0) is False
    assert registry.frames == []


async def test_the_completed_write_is_then_announced(tmp_path):
    _write_model(tmp_path, "part.stl")
    watcher, registry = _watcher(tmp_path)
    await _prime(watcher)

    (tmp_path / "part.stl").write_bytes(
        b"\0" * 80 + struct.pack("<I", 40) + _stl_bytes(2)[84:])
    assert await watcher.tick(1.0) is False

    _write_model(tmp_path, "part.stl", triangles=6)
    assert await watcher.tick(1.1) is True


async def test_a_model_that_never_settles_is_announced_at_the_deadline(tmp_path):
    """A file being rewritten continuously never looks settled, and a watcher
    that waits for quiet that never comes is a watcher that never fires."""
    _write_model(tmp_path, "part.stl")
    watcher, registry = _watcher(tmp_path, max_defer=2.0)
    await _prime(watcher)

    truncated = b"\0" * 80 + struct.pack("<I", 40) + _stl_bytes(2)[84:]
    (tmp_path / "part.stl").write_bytes(truncated)
    assert await watcher.tick(1.0) is False    # deferral starts
    assert await watcher.tick(2.0) is False    # still inside the window
    assert await watcher.tick(3.5) is True     # past max_defer, announced anyway


async def test_a_removal_is_not_deferred(tmp_path):
    """Nothing is left to parse, so there is nothing to wait for."""
    _write_model(tmp_path, "part.stl")
    _write_model(tmp_path, "clip.stl")
    watcher, _registry = _watcher(tmp_path, max_defer=30.0)
    await _prime(watcher)

    (tmp_path / "clip.stl").unlink()
    assert await watcher.tick(1.0) is True


# -- the signature itself ---------------------------------------------------


def test_the_signature_covers_size_mtime_and_a_digest(tmp_path):
    _write_model(tmp_path, "part.stl")
    signature = _models_signature(tmp_path)
    assert list(signature) == ["part.stl"]
    size, mtime_ns, digest = signature["part.stl"]
    st = (tmp_path / "part.stl").stat()
    assert (size, mtime_ns) == (st.st_size, st.st_mtime_ns)
    assert digest, "a freshly written model is digested, since its mtime proves nothing yet"


def test_a_settled_model_is_not_rehashed(tmp_path):
    """The steady state costs no reads: a model whose timestamp is old enough to
    be conclusive carries its previous digest forward untouched."""
    _write_model(tmp_path, "part.stl")
    first = _models_signature(tmp_path)
    # Pretend the poll is happening well after the write.
    later = time.time() + 3600
    carried = _models_signature(tmp_path, previous=first, now=later)
    assert carried == first

    sentinel = {"part.stl": first["part.stl"][:2] + ("carried-forward",)}
    reused = _models_signature(tmp_path, previous=sentinel, now=later)
    assert reused["part.stl"][2] == "carried-forward", "it re-read a settled model"


def test_a_same_size_same_mtime_rewrite_is_still_detected(tmp_path):
    """The case size and mtime cannot see, and the reason a digest exists at all.

    A parametric edit that moves coordinates without changing the triangle count
    preserves the file size exactly, and two writes landing inside one
    filesystem timestamp granule share an mtime to the nanosecond. Both happen
    here, so this fails outright against a purely stat-based signature.
    """
    model = tmp_path / "part.stl"
    _write_model(tmp_path, "part.stl", triangles=4, z=0.0)
    before = _models_signature(tmp_path)
    original = model.stat()

    _write_model(tmp_path, "part.stl", triangles=4, z=5.0)
    # Timestamp forced back, rather than relying on both writes happening to
    # land inside one granule: whether they do depends on the filesystem and on
    # how busy the machine is, and a test that only sometimes exercises the case
    # it was written for is a test that only sometimes exists.
    os.utime(model, ns=(original.st_atime_ns, original.st_mtime_ns))

    after = _models_signature(tmp_path, previous=before)
    assert before["part.stl"][:2] == after["part.stl"][:2], (
        "size and mtime must be identical for this to be testing anything")
    assert before != after, "the digest is what has to notice this"
