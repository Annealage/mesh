"""Mesh's own tools reaching a Claude session's allow list.

How ``SdkSession`` drives the real ``ClaudeSDKClient``, and how ``launch.py``
hands a product's tool server to it, is ``annealage_agent``'s own suite,
exercised there against its toy product. What stays here is the one fact that
suite cannot state: that the list Mesh's own tools arrive at the session with
is exactly the read- and view-grade Mesh tools, and none of the five that
leave something on disk.
"""

from types import SimpleNamespace

from annealage_agent import launch, sessions
from annealage_agent import settings as settings_module

from annealage_mesh.tools.registry import MeshTools

# The mesh tools that never prompt, in the namespaced form the Claude SDK
# matches an allow rule against (``mcp__<server>__<tool>``). Hardcoded here
# rather than read off the tool server: this test exists to notice if the list
# the session is built with drifts, so it must not share its source of truth
# with the code it is pinning.
EXPECTED_PRE_ALLOWED_TOOLS = [
    # Read-class: changes nothing.
    "mcp__mesh__list_models",
    "mcp__mesh__model_info",
    "mcp__mesh__get_view",
    "mcp__mesh__get_visibility",
    "mcp__mesh__list_comments",
    "mcp__mesh__list_callouts",
    "mcp__mesh__capture_view",
    "mcp__mesh__measure",
    "mcp__mesh__mesh_verify",
    "mcp__mesh__mesh_dimensions",
    # View-class: changes only what is on the screen the human is watching, so
    # the pause switch is the control rather than a card per camera move.
    "mcp__mesh__set_view",
    "mcp__mesh__fit_view",
    "mcp__mesh__set_visibility",
    "mcp__mesh__set_up_axis",
    "mcp__mesh__select_pin",
]

# The five that leave something on disk, and therefore must NOT be here. Listed
# so this file states the negative rather than leaving it to be inferred from
# what is missing above: one of these appearing in ``allowed_tools`` would
# silently remove the human's approval card and nothing else would notice.
EXPECTED_NEVER_PRE_ALLOWED = [
    "mcp__mesh__add_callout",
    "mcp__mesh__delete_callout",
    "mcp__mesh__snapshot",
    "mcp__mesh__export_transcript",
    "mcp__mesh__mesh_dimensions_set",
]


def test_launch_builds_the_claude_session_with_the_tool_servers_pre_allowed_list(tmp_path):
    """The production path, not a list this test hands the session itself:
    ``launch.py`` building a Claude session from Mesh's tool server on the
    bus. Its allow list is exactly the read- and view-grade tools, namespaced,
    and no write-grade tool, whose absence is what makes each of them reach
    the human as a card."""

    class _StubBus:
        paused = False

    session_id = sessions.create_session(tmp_path)
    bus = SimpleNamespace(
        tools=MeshTools(_StubBus(), tmp_path, session_id),
        broker=None,
        url="http://127.0.0.1:8765/",
    )
    session = launch.build_session(
        "claude",
        lambda event: None,
        bus=bus,
        serve_dir=tmp_path,
        session_id=session_id,
        resumed=False,
        settings=settings_module.resolve(tmp_path),
        mcp_host="127.0.0.1",
        mcp_port=8765,
        agent_token="agent",
    )
    allowed = session._build_options().allowed_tools
    assert allowed == EXPECTED_PRE_ALLOWED_TOOLS
    for name in EXPECTED_NEVER_PRE_ALLOWED:
        assert name not in allowed
