"""Tests for the change signal the models watcher polls with.

``_models_signature`` is a pure function over a directory, and the whole point
of it is what it costs and what it can still notice: an STL is routinely
megabytes and the watcher samples several times a second, so hashing everything
every tick would be wasteful, while hashing nothing would miss a rewrite that
lands on the same size and the same nanosecond. The signature is therefore size
and modification time, with a digest computed only when those two cannot settle
the question.

Separate from ``tests/test_models_watcher.py`` because the subject is separate:
that file asserts what the watcher does with a signature across ticks, and this
one asserts what a signature is. Needing no event loop follows from that rather
than being the reason for it.
"""

import os
import struct
import time

from annealage_mesh.app import _models_signature


def _stl_bytes(triangles=2, z=0.0):
    """A structurally complete binary STL with ``triangles`` records."""
    out = [b"\0" * 80, struct.pack("<I", triangles)]
    for i in range(triangles):
        out.append(
            struct.pack("<12fH", 0.0, 0.0, 1.0, 0.0, 0.0, z, 1.0 + i, 0.0, z, 0.0, 1.0, z, 0)
        )
    return b"".join(out)


def _write_model(serve_dir, name, triangles=2, z=0.0):
    (serve_dir / name).write_bytes(_stl_bytes(triangles, z))


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
        "size and mtime must be identical for this to be testing anything"
    )
    assert before != after, "the digest is what has to notice this"
