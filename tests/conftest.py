"""Fixtures shared by the HTTP and path-scanning test suites.

``served_dir`` and ``client`` are the smallest served-directory shape (one
top-level model), used by most tests. ``nested_served_dir`` builds a deeper
tree exercising several recursive-scan rules at once (a model nested several
directories down, a dotdir holding a model that must never surface, and a
symlinked directory aliasing a real one), so that shape is built once here
rather than inline in every test that needs it.
"""

import pytest
from microdot.test_client import TestClient

from annealage_mesh.app import create_app


@pytest.fixture
def served_dir(tmp_path):
    (tmp_path / "widget.stl").write_bytes(b"solid widget\nendsolid widget\n")
    return tmp_path


@pytest.fixture
def client(served_dir):
    return TestClient(create_app(served_dir))


@pytest.fixture
def nested_served_dir(tmp_path):
    (tmp_path / "top.stl").write_bytes(b"solid top\nendsolid top\n")

    deep = tmp_path / "models" / "a" / "sub" / "deeper"
    deep.mkdir(parents=True)
    (deep / "pin.stl").write_bytes(b"solid pin\nendsolid pin\n")

    dotdir = tmp_path / ".git"
    dotdir.mkdir()
    (dotdir / "hidden.stl").write_bytes(b"solid hidden\nendsolid hidden\n")

    real_dir = tmp_path / "real"
    real_dir.mkdir()
    (real_dir / "real.stl").write_bytes(b"solid real\nendsolid real\n")
    (tmp_path / "linked").symlink_to(real_dir, target_is_directory=True)

    return tmp_path
