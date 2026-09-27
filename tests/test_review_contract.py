"""D9: the published two-file comment contract keeps working now that the
review model lives in the agent layer.

``mesh-comments.json`` and ``mesh-callouts.json`` are documented in the README
and in the MIT skill (``skill/annealage-mesh/SKILL.md``), and a separately
running agent reads and writes them directly, so they are a contract Mesh
cannot change by moving code. Nor can it change what its review tools say to
the model, which a conversation already under way has learnt to read.

So the tool results below are pinned byte for byte against what Mesh at
406b074 produced for the same calls (``fixtures/review_tools_406b074.json``,
captured by running that commit's tools), and the file shapes against both
that capture and the documents. Alongside: an external agent's direct write is
still seen by the tools, the viewer's route and the push; the callout limit is
still 200; and the shared model reads the published files the way this
package's own docstring says it does.
"""

import json
from pathlib import Path

import pytest
from annealage_agent.review import ReviewWatcher
from annealage_agent.tools import namespaced
from conftest import TEST_HOST, make_test_client

from annealage_mesh import paths, review
from annealage_mesh.app import DEFAULT_PORT, create_app
from annealage_mesh.tools import registry

pytestmark = pytest.mark.asyncio

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "review_tools_406b074.json").read_text(encoding="utf-8")
)
REVIEW_TOOLS = ("list_comments", "list_callouts", "add_callout", "delete_callout")
TOKEN = "contract-browser-token"

# The two documents' examples, verbatim: SKILL.md's (which the capture used)
# and the README's.
SKILL_COMMENTS = {
    "submitted_at": "2026-07-23T10:15:00+00:00",
    "count": 1,
    "annotations": [
        {
            "id": 1,
            "part": "shroud",
            "label": "+Z",
            "point": [12.4, -3.1, 8.0],
            "normal": [0.0, 0.0, 1.0],
            "faceIndex": 1523,
            "comment": "this wall looks too thin",
        }
    ],
}
SKILL_CALLOUTS = {
    "annotations": [
        {
            "id": 1,
            "author": "agent",
            "part": "shroud",
            "label": "+Z",
            "point": [12.4, -3.1, 8.0],
            "comment": "widened this wall 1.2mm -> 2mm, please confirm clearance",
        }
    ]
}
README_COMMENT = {
    "id": 1,
    "part": "bracket",
    "label": "+Z",
    "point": [12.5, -3.2, 44.0],
    "normal": [0, 0, 1],
    "faceIndex": 1234,
    "comment": "this fillet's too sharp",
}
README_CALLOUT = {
    "id": 1,
    "author": "agent",
    "part": "bracket",
    "label": "+Y",
    "point": [0, 20, 10],
    "comment": "moved this wall out 2mm, that clear enough?",
}


class FakeBus:
    def __init__(self):
        self.paused = False

    async def call(self, method, params=None, *, timeout=None):
        return {}


@pytest.fixture
def project(tmp_path):
    return tmp_path.resolve()


def _tools(project):
    return registry.MeshTools(FakeBus(), project)


def _handlers(project):
    return {t.name: t.handler for t in _tools(project).tools}


def _seen(result):
    """A tool result as the model reads it, in the capture's own form."""
    return {"is_error": result.get("is_error", False), "text": result["content"][0]["text"]}


def _expected(name, project):
    """A captured result or file, with this test's served directory in it."""
    value = json.dumps(FIXTURE[name]).replace("{serve_dir}", str(project))
    return json.loads(value)


def _callouts(project):
    return project / paths.CALLOUTS_JSON_NAME


def _comments(project):
    return project / paths.COMMENTS_JSON_NAME


# --- what the model is told the tools are ---------------------------------------


async def test_the_review_tools_are_described_to_the_model_as_before(project):
    tools = _tools(project)
    assert [t.name for t in tools.tools] == FIXTURE["order"]
    table = tools.tool_table()
    for tool_def in tools.tools:
        if tool_def.name not in REVIEW_TOOLS:
            continue
        before = FIXTURE["meta"][tool_def.name]
        declared = tool_def.input_schema
        if any(isinstance(v, type) for v in declared.values()):
            declared = {k: v.__name__ for k, v in declared.items()}
        assert tool_def.description == before["description"], tool_def.name
        assert json.dumps(declared) == json.dumps(before["input_schema"]), tool_def.name
        assert json.dumps(table[tool_def.name].schema) == json.dumps(before["json_schema"])


async def test_the_grades_are_as_before_and_nothing_resolves(project):
    """Adding and deleting a callout stay write-grade (each a card), the two
    lists read-grade, and there is no resolve tool: neither file has a
    status."""
    tools = _tools(project)
    assert "resolve_comment" not in {t.name for t in tools.tools}
    for name in ("add_callout", "delete_callout"):
        assert name in registry.GRADING.write
        assert namespaced("mesh", name) not in tools.pre_allowed
    for name in ("list_comments", "list_callouts"):
        assert namespaced("mesh", name) in tools.pre_allowed


# --- what the model reads -----------------------------------------------------------


async def test_list_comments_reads_as_before(project):
    list_comments = _handlers(project)["list_comments"]
    assert _seen(await list_comments({})) == _expected("list_comments_absent", project)
    _comments(project).write_text(json.dumps(SKILL_COMMENTS, indent=2) + "\n")
    assert _seen(await list_comments({})) == _expected("list_comments_skill", project)
    _comments(project).write_text(json.dumps({"submitted_at": "x", "count": 0, "annotations": []}))
    assert _seen(await list_comments({})) == _expected("list_comments_empty", project)
    _comments(project).write_text("{ nope")
    assert _seen(await list_comments({})) == _expected("list_comments_unparseable", project)


async def test_list_callouts_reads_as_before(project):
    list_callouts = _handlers(project)["list_callouts"]
    assert _seen(await list_callouts({})) == _expected("list_callouts_absent", project)
    _callouts(project).write_text(json.dumps(SKILL_CALLOUTS, indent=2) + "\n")
    assert _seen(await list_callouts({})) == _expected("list_callouts_skill", project)
    # The bare array the viewer also accepts.
    _callouts(project).write_text(json.dumps(SKILL_CALLOUTS["annotations"]))
    assert _seen(await list_callouts({})) == _expected("list_callouts_bare", project)
    _callouts(project).write_text("nonsense")
    assert _seen(await list_callouts({})) == _expected("list_callouts_unparseable", project)


async def test_the_readme_s_examples_are_shown_to_the_model_verbatim(project):
    _comments(project).write_text(
        json.dumps({"submitted_at": "...", "count": 1, "annotations": [README_COMMENT]})
    )
    _callouts(project).write_text(json.dumps({"annotations": [README_CALLOUT]}))
    handlers = _handlers(project)
    comments = json.loads(_seen(await handlers["list_comments"]({}))["text"])
    callouts = json.loads(_seen(await handlers["list_callouts"]({}))["text"])
    assert comments == {"submitted_at": "...", "count": 1, "annotations": [README_COMMENT]}
    assert callouts == {"count": 1, "annotations": [README_CALLOUT]}


# --- what the model writes, and the files it leaves ----------------------------


async def test_add_and_delete_write_the_published_shape_as_before(project):
    handlers = _handlers(project)
    first = await handlers["add_callout"](
        {
            "point": [1, 2.5, 3],
            "comment": "  moved this wall out 2mm  ",
            "part": "bracket",
            "label": "+Y",
        }
    )
    assert _seen(first) == _expected("add_first", project)
    assert _callouts(project).read_text() == _expected("add_first_file", project)

    _callouts(project).write_text(json.dumps(SKILL_CALLOUTS, indent=2) + "\n")
    second = await handlers["add_callout"](
        {"point": ["0", 0, 1e3], "comment": "new", "part": "  ", "label": 5}
    )
    assert _seen(second) == _expected("add_to_skill", project)
    assert _callouts(project).read_text() == _expected("add_to_skill_file", project)

    deleted = await handlers["delete_callout"]({"id": 1})
    assert _seen(deleted) == _expected("delete_1", project)
    assert _callouts(project).read_text() == _expected("delete_1_file", project)
    for args, name in (
        ({"id": 9}, "delete_missing"),
        ({"id": True}, "delete_bool"),
        ({"id": "2"}, "delete_str"),
    ):
        assert _seen(await handlers["delete_callout"](args)) == _expected(name, project)


@pytest.mark.parametrize(
    "args, name",
    [
        ({"point": [1, 2], "comment": "x"}, "add_bad_point_len"),
        ({"point": "1,2,3", "comment": "x"}, "add_bad_point_type"),
        ({"point": [1, "a", 3], "comment": "x"}, "add_bad_point_values"),
        ({"comment": "x"}, "add_no_point"),
        ({"point": [1, 2, 3], "comment": "  "}, "add_empty_comment"),
        ({"point": [1, 2, 3]}, "add_no_comment"),
    ],
)
async def test_a_bad_callout_is_refused_as_before(project, args, name):
    assert _seen(await _handlers(project)["add_callout"](args)) == _expected(name, project)
    assert not _callouts(project).exists()


async def test_a_callouts_file_that_does_not_parse_is_refused_and_untouched(project):
    _callouts(project).write_text("{ this is not json")
    handlers = _handlers(project)
    added = await handlers["add_callout"]({"point": [0, 0, 1], "comment": "mine"})
    deleted = await handlers["delete_callout"]({"id": 1})
    assert _seen(added) == _expected("add_unparseable", project)
    assert _seen(deleted) == _expected("delete_unparseable", project)
    assert _callouts(project).read_text() == "{ this is not json"


async def test_the_callout_limit_is_still_200(project):
    assert review.MAX_CALLOUTS == 200
    assert review.MeshFilesStore(project).capabilities.max_open_model_callouts == 200
    add = _handlers(project)["add_callout"]
    full = [{"id": i, "point": [0, 0, 0], "comment": "c%d" % i} for i in range(1, 201)]
    _callouts(project).write_text(json.dumps({"annotations": full}))
    assert _seen(await add({"point": [0, 0, 1], "comment": "mine"})) == _expected(
        "add_at_cap", project
    )
    _callouts(project).write_text(json.dumps({"annotations": full[:199]}))
    assert _seen(await add({"point": [0, 0, 1], "comment": "mine"})) == _expected(
        "add_below_cap", project
    )


async def test_a_symlinked_callouts_file_is_read_as_absent_and_never_written(project):
    outside = project.parent / ("outside-%s.json" % project.name)
    outside.write_text("{}")
    _callouts(project).symlink_to(outside)
    handlers = _handlers(project)
    added = await handlers["add_callout"]({"point": [0, 0, 1], "comment": "mine"})
    listed = await handlers["list_callouts"]({})
    assert _seen(added) == _expected("add_symlink", project)
    assert _seen(listed) == _expected("list_callouts_symlink", project)
    assert outside.read_text() == "{}"


# --- an agent writing the file directly, as the contract allows -------------------


async def test_a_direct_write_is_seen_by_the_tools_the_route_and_the_push(project):
    events = []
    watcher = ReviewWatcher(review.MeshFilesStore(project), events.append)
    await watcher.tick(0.0)

    raw = json.dumps({"annotations": [README_CALLOUT]}, indent=4)
    _callouts(project).write_text(raw)

    listed = json.loads(_seen(await _handlers(project)["list_callouts"]({}))["text"])
    assert listed["annotations"] == [README_CALLOUT]
    res = await make_test_client(create_app(project, host=TEST_HOST, port=DEFAULT_PORT)).get(
        "/callouts"
    )
    assert res.body.decode("utf-8") == raw, "the viewer's route serves the file verbatim"
    assert await watcher.tick(1.0) is True
    assert [event.kind for event in events] == ["review_changed"]


async def test_a_submit_is_read_back_by_list_comments_unchanged(project):
    """The page's Submit writes the human's pins in its own shape, and the
    tool hands that record to the model as it stands."""
    client = make_test_client(create_app(project, host=TEST_HOST, port=DEFAULT_PORT))
    pins = [dict(README_COMMENT, id=1), dict(README_COMMENT, id=2, comment="and this one")]
    res = await client.post(
        "/submit", headers={"Content-Type": "application/json"}, body=json.dumps(pins)
    )
    assert res.status_code == 200
    record = json.loads(_comments(project).read_text())
    assert set(record) == {"submitted_at", "count", "annotations"}
    assert record["count"] == 2 and record["annotations"] == pins
    listed = json.loads(_seen(await _handlers(project)["list_comments"]({}))["text"])
    assert listed == record


# --- the shared model's reading of the two files --------------------------------


async def test_get_review_maps_both_files_onto_the_shared_model(project):
    """How ``review.py`` says the files map: the comments file is the human's,
    the callouts file the model's whatever its author field says, the anchor
    is part, point, normal and faceIndex, the face label is an extra, ids are
    per file, and nothing has a status."""
    _comments(project).write_text(
        json.dumps({"submitted_at": "t", "count": 1, "annotations": [README_COMMENT]})
    )
    _callouts(project).write_text(json.dumps({"annotations": [dict(README_CALLOUT, author="bob")]}))
    app = create_app(project, host=TEST_HOST, port=DEFAULT_PORT, token=TOKEN)
    res = await make_test_client(app).get("/review?t=%s" % TOKEN)
    body = json.loads(res.body.decode("utf-8"))
    assert body["anchor_space"] == "mesh-point"
    assert body["capabilities"] == {
        "can_resolve": False,
        "can_delete_own": True,
        "human_adds_via_api": False,
        "human_sets_status": False,
        "can_update_anchors": False,
        "max_open_model_callouts": 200,
    }
    assert body["comments"] == [
        {
            "id": 1,
            "anchor": {
                "part": "bracket",
                "point": [12.5, -3.2, 44.0],
                "normal": [0, 0, 1],
                "faceIndex": 1234,
            },
            "text": "this fillet's too sharp",
            "author": "human",
            "extra": {"label": "+Z"},
        },
        {
            "id": 1,
            "anchor": {"part": "bracket", "point": [0, 20, 10]},
            "text": "moved this wall out 2mm, that clear enough?",
            "author": "model",
            "extra": {"label": "+Y"},
        },
    ]


async def test_the_page_adds_pins_by_submit_not_through_the_review_route(project):
    app = create_app(project, host=TEST_HOST, port=DEFAULT_PORT, token=TOKEN)
    res = await make_test_client(app).post(
        "/review?t=%s" % TOKEN,
        headers={"Content-Type": "application/json"},
        body=json.dumps({"anchor": {"point": [0, 0, 0]}, "text": "draft"}),
    )
    assert res.status_code == 405
    assert not _comments(project).exists()
