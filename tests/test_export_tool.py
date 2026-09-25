"""Mesh's ``export_transcript`` tool: the model's way to write the conversation
into ``review/``.

The human's Export button, ``POST /session/<sid>/export``, is the agent
layer's route and is tested in annealage-agent's suite, as are the rendering
and the containment of the write (its ``tests/test_export.py``). The tool is
Mesh's: it is write-class, so it is absent from every allow list and reaches
the broker (asserted in ``tests/test_tools.py``); what is asserted here is
that it writes what the route writes, and refuses cleanly, in words the model
can act on, when there is no session to export or its arguments are wrong.
"""

import json

import pytest
from annealage_agent import files, sessions
from annealage_agent.session import events
from annealage_agent.session.base import TextDelta, ToolUse

from annealage_mesh.tools import registry

pytestmark = pytest.mark.asyncio


class StubBus:
    """A bus that answers nothing, which is all ``export_transcript`` needs.

    It touches no viewer at all: the conversation it writes out comes from the
    event log on disk, so a tool test needs a bus only because every mesh tool
    server is built with one.
    """

    paused = False
    url = "http://127.0.0.1:8765/#t=stub"

    async def call(self, method, params=None, *, timeout=None):
        raise AssertionError("export_transcript must not call the viewer: %s" % method)


@pytest.fixture
def project(served_dir):
    """A served directory with one session holding a short conversation."""
    sid = sessions.create_session(served_dir)
    log = events.EventLog(str(sessions.events_path(served_dir, sid)))
    log.append(TextDelta(turn=1, text="the boss wall is 2mm too thin"))
    log.append(
        ToolUse(turn=1, tool_use_id="t1", name="mcp__mesh__set_view", input={"position": [1, 2, 3]})
    )
    log.close()
    return served_dir, sid


def _tool(serve_dir, session_id):
    handlers = {
        t.name: t.handler for t in registry.MeshTools(StubBus(), serve_dir, session_id).tools
    }
    return handlers["export_transcript"]


def text_of(result):
    for item in result["content"]:
        if item["type"] == "text":
            return item["text"]
    return ""


async def test_tool_writes_a_transcript_and_reports_a_project_relative_path(project):
    served_dir, sid = project
    result = await _tool(served_dir, sid)({})
    assert not result.get("is_error")
    payload = json.loads(text_of(result))
    assert payload["path"].startswith("%s/transcript-" % files.REVIEW_DIRNAME)
    assert (served_dir / payload["path"]).is_file()
    assert payload["format"] == "markdown"


async def test_tool_refuses_when_there_is_no_session_to_export(served_dir):
    """A viewer-only run builds the tool because the classification requires
    every classified tool to exist, so the refusal has to happen here, and it
    has to tell the model not to retry."""
    result = await _tool(served_dir, None)({})
    assert result["is_error"] is True
    assert "no session to export" in text_of(result)
    assert not (served_dir / files.REVIEW_DIRNAME).exists()


@pytest.mark.parametrize(
    "args,fragment",
    [
        ({"format": "md"}, "format must be one of"),
        ({"include": "all"}, "include must be one of"),
    ],
)
async def test_tool_argument_refusals_name_the_allowed_values(project, args, fragment):
    """A refusal's text reaches the model verbatim, so naming the values it may
    use is what lets it fix the call instead of abandoning the tool."""
    served_dir, sid = project
    result = await _tool(served_dir, sid)(args)
    assert result["is_error"] is True
    assert fragment in text_of(result)


async def test_tool_reports_a_containment_refusal_without_retry_advice(project, tmp_path):
    served_dir, sid = project
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (served_dir / files.REVIEW_DIRNAME).symlink_to(outside, target_is_directory=True)
    result = await _tool(served_dir, sid)({})
    assert result["is_error"] is True
    assert "rather than retrying" in text_of(result)
    assert list(outside.iterdir()) == []
