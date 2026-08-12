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

from annealage_mesh.app import DEFAULT_PORT, create_app

# Every app validates the inbound Host header against the address it is bound
# to, so a test client has to send one that truthfully names this app or every
# request is refused before it reaches a route. microdot's TestClient
# otherwise defaults to "example.com:1234", which is exactly the mismatched
# name that check exists to refuse.
TEST_HOST = "127.0.0.1"
TEST_AUTHORITY = "%s:%d" % (TEST_HOST, DEFAULT_PORT)


def make_test_client(app):
    """A TestClient whose Host header names the bind ``app`` was built for."""
    return TestClient(app, host=TEST_AUTHORITY)


@pytest.fixture
def served_dir(tmp_path):
    (tmp_path / "widget.stl").write_bytes(b"solid widget\nendsolid widget\n")
    return tmp_path


@pytest.fixture
def client(served_dir):
    return make_test_client(create_app(served_dir, host=TEST_HOST, port=DEFAULT_PORT))


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
