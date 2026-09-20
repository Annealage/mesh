"""On-demand live integration test for ``session/omp.py``: proves the real
``omp_rpc.RpcClient`` (not ``tests/test_omp_session.py``'s ``FakeRpcClient``)
can authenticate against a real, titan-hosted OpenAI-compatible endpoint and
complete one real text-only turn (plan: ``phase7_live-integration-tests.md``).

Skips at collection time, with a clear reason, unless both
``MESH_LIVE_OMP_BASE_URL`` and ``MESH_LIVE_OMP_EXECUTABLE`` are set -- see
that ticket's env-var table. Excluded from every default ``pytest`` run by
``pyproject.toml``'s ``addopts = "-m 'not integration'"``; run this tier
explicitly with ``pytest -m integration``.

``OmpSession.start()`` hardcodes ``executable="omp"`` with no constructor
override, resolved via the launched subprocess's own ``PATH`` -- the same
seam ``tests/test_diagnostics.py``'s ``_omp_info`` tests exercise for the
same binary, PATH lookup being the only seam that exists here since
``session/omp.py`` never calls ``shutil.which`` itself. This test prepends a
scratch directory holding a symlink literally named ``omp`` (pointing at
``MESH_LIVE_OMP_EXECUTABLE``'s target) onto ``PATH`` rather than adding an
``executable=`` parameter to ``OmpSession``, keeping this test provably
using whichever binary a human explicitly named rather than whatever ``omp``
a bare PATH lookup would have found ambiently.

**omp model naming.** ``OmpSession.start()`` always constructs
``model="%s/%s" % (_PROVIDER_ID, model_id)`` (``_PROVIDER_ID`` is the
hardcoded ``"mesh-local"`` string mesh's own custom-provider config uses
internally -- confirmed against ``tests/test_omp_session.py``:
``fake.kwargs["model"] == "mesh-local/llama3.1:8b"`` for
``OmpSession(model="llama3.1:8b", ...)``). The ``model=`` constructor
argument passed below is therefore always the *bare* model id the endpoint
itself understands, never a ``<provider>/<model>`` string -- passing
``"titan/qwen3.8-27b"`` would double-prefix into
``"mesh-local/titan/qwen3.8-27b"``, which no real endpoint answers to.
"titan" names which real host serves the model, i.e. it is
``MESH_LIVE_OMP_BASE_URL``'s concern, not ``MESH_LIVE_OMP_MODEL``'s --
``MESH_LIVE_OMP_MODEL`` defaults to the bare ``"qwen3.8-27b"``.
"""

import asyncio
import os

import pytest

from annealage_mesh.session.base import (
    AGENT_READY,
    AgentError,
    TextDelta,
    TurnEnd,
)
from annealage_mesh.session.omp import OmpSession
from annealage_mesh.session.permissions import PermissionBroker

LIVE_PROMPT = "Reply with exactly the single word PONG and nothing else, no punctuation."


def _reply_text(events):
    return "".join(event.text for event in events if isinstance(event, TextDelta))


def _diagnose(events):
    """A human-readable dump of every event collected so far, for an
    assertion failure message -- a live backend's real stderr/remediation
    is what tells a developer whether a failure was auth, network, an
    unsupported model id, or an unexpected reply, not a bare `False`."""
    lines = ["%d event(s) collected:" % len(events)]
    for event in events:
        lines.append("  %r" % (event,))
    return "\n".join(lines)


async def _wait_for_turn_end(events, *, timeout):
    """Poll ``events`` for a ``TurnEnd``, failing fast on an ``AgentError``
    instead of waiting out the full timeout when the backend has already
    reported it cannot continue."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    seen = 0
    while True:
        while seen < len(events):
            event = events[seen]
            seen += 1
            if isinstance(event, AgentError):
                raise AssertionError("agent reported an error: %s" % _diagnose(events))
            if isinstance(event, TurnEnd):
                return event
        if loop.time() >= deadline:
            raise AssertionError("timed out waiting for turn_end: %s" % _diagnose(events))
        await asyncio.sleep(0.05)


@pytest.mark.integration
@pytest.mark.skipif(
    not os.environ.get("MESH_LIVE_OMP_BASE_URL") or not os.environ.get("MESH_LIVE_OMP_EXECUTABLE"),
    # `.get(...)` (falsy checks), not `"X" not in os.environ`: GitHub
    # Actions expands an unset secret to an empty string rather than
    # omitting the env var entirely, so a presence check alone would let a
    # dispatch with no MESH_LIVE_OMP_BASE_URL/MESH_LIVE_OMP_EXECUTABLE
    # secret configured attempt a real run instead of skipping.
    reason="set MESH_LIVE_OMP_BASE_URL and MESH_LIVE_OMP_EXECUTABLE to run the live omp test",
)
@pytest.mark.asyncio
async def test_real_omp_backend_completes_a_turn(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "omp").symlink_to(os.environ["MESH_LIVE_OMP_EXECUTABLE"])
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ["PATH"])
    # `or "qwen3.8-27b"`, not `.get(..., "qwen3.8-27b")`: the same
    # empty-string-from-GitHub-Actions concern applies to the optional
    # override. Bare model id -- see this module's "omp model naming"
    # docstring note; never the "titan/..." form. OmpSession.start()
    # prefixes it with its own hardcoded "mesh-local" provider id before
    # ever reaching `omp`.
    model = os.environ.get("MESH_LIVE_OMP_MODEL") or "qwen3.8-27b"

    events = []
    session = OmpSession(
        events.append,
        cwd=str(tmp_path),
        session_id="live-omp",
        broker=PermissionBroker(events.append, timeout=30.0, no_viewer_grace=0.05),
        model=model,
        base_url=os.environ["MESH_LIVE_OMP_BASE_URL"],
        # `or None`: an empty-string secret (GitHub Actions' expansion of
        # an unset one) must mean "keyless endpoint", not "send an empty
        # Authorization header" - see session/omp.py's own module docstring
        # on why `api_key=None` is the deliberate keyless case.
        api_key=os.environ.get("MESH_LIVE_OMP_API_KEY") or None,
    )
    session.on_viewer_presence(1)
    try:
        await session.start()
        assert session.agent_status() == AGENT_READY, _diagnose(events)
        await session.submit_turn([{"type": "text", "text": LIVE_PROMPT}])
        await _wait_for_turn_end(events, timeout=60.0)
    finally:
        await session.close()
    assert "pong" in _reply_text(events).lower(), _diagnose(events)
