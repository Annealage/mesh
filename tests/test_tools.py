"""Tests for the mesh tool surface, driven against a fake ``ViewerBus``.

The tool server's own mechanisms (how a grading derives what is pre-allowed
and what the pause switch refuses, the refusal of an unclassified tool, and
the mapping of a viewer call's four failures) are tested in annealage-agent's
suite against a toy product. What is pinned here is Mesh's use of them, and
Mesh's handlers.

**The classification**, because it is the whole permission design for these
tools, and because its two derived sets are deliberately not the same set: what
prompts is the write-class tools, while what the pause switch refuses is those
plus the five that change the view. A test that checked only one of those would
pass with the other silently wrong, so both are asserted, exhaustively. The
expected tuples below are written out by hand rather than imported from the
code, for the same reason ``tests/test_sdk_session.py`` writes out the allow
list: a test that derives its expectation from the thing it is testing cannot
notice that thing changing.

**The pause gate**, exhaustively over every Mesh tool that changes anything
rather than over a sample, each refused before it touches the viewer or the
disk. A tool added later without the gate would be a hole exactly the size of
that tool, and a spot check would not find it.

**The handlers**: what each sends to the viewer and writes to the served
directory, and what it tells the model when its arguments or the files it
reads are wrong.

Every handler here is reached through ``MeshTools``, never called directly, so
what is under test includes the wrapper that applies both policies.
"""

import base64
import json
import os
import struct

import pytest
from annealage_agent import files
from annealage_agent.tools import namespaced

from annealage_mesh import paths
from annealage_mesh.tools import registry

pytestmark = pytest.mark.asyncio


# The three grades, written out by hand so that adding a tool without deciding
# its grade fails here rather than defaulting into a posture nobody chose.
EXPECTED_READ_CLASS = (
    "list_models",
    "model_info",
    "get_view",
    "get_visibility",
    "list_comments",
    "list_callouts",
    "capture_view",
    "measure",
    "mesh_verify",
    "mesh_dimensions",
)
EXPECTED_VIEW_CLASS = (
    "set_view",
    "fit_view",
    "set_visibility",
    "set_up_axis",
    "select_pin",
)
EXPECTED_WRITE_CLASS = (
    "add_callout",
    "delete_callout",
    "snapshot",
    "export_transcript",
    "mesh_dimensions_set",
)

# Never prompts: nothing here reaches the broker, so nothing here interrupts.
EXPECTED_PRE_ALLOWED = EXPECTED_READ_CLASS + EXPECTED_VIEW_CLASS
# Refused while paused: everything that changes anything, screen or disk.
EXPECTED_PAUSE_GATED = EXPECTED_VIEW_CLASS + EXPECTED_WRITE_CLASS

# Plausible arguments per tool, so the pause and mapping tests can drive every
# one of them without each needing its own call. These are not assertions about
# the schemas; they are the smallest arguments each tool accepts.
ARGS = {
    "list_models": {},
    "model_info": {"rel": "cube.stl"},
    "get_view": {},
    "get_visibility": {},
    "list_comments": {},
    "list_callouts": {},
    "capture_view": {},
    "measure": {"a": "pin:1", "b": "pin:2"},
    "mesh_verify": {"rel": "cube.stl"},
    "mesh_dimensions": {"action": "read"},
    "mesh_dimensions_set": {"key": "width", "value": "42"},
    "set_view": {"target": [1, 2, 3]},
    "fit_view": {},
    "set_visibility": {"rel": "cube.stl", "visible": False},
    "set_up_axis": {"axis": "y"},
    "add_callout": {"point": [1, 2, 3], "comment": "here"},
    "delete_callout": {"id": 1},
    "select_pin": {"pin": 1},
    "snapshot": {},
    "export_transcript": {},
}


def _cube_stl(half=5.0):
    """A valid binary STL of two triangles, enough for real geometry facts."""
    header = b"mesh test cube".ljust(80, b"\x00")
    triangles = [
        (0, 0, 1, -half, -half, half, half, -half, half, half, half, half),
        (0, 0, 1, -half, -half, half, half, half, half, -half, half, half),
    ]
    data = header + struct.pack("<I", len(triangles))
    for triangle in triangles:
        data += struct.pack("<12fH", *triangle, 0)
    return data


def _png_data_url(payload=b"not really a png, but real base64"):
    return "data:image/png;base64," + base64.b64encode(payload).decode("ascii")


class FakeBus:
    """A recorder with the two members every tool handler depends on.

    The real ``ViewerBus`` exposes ``paused`` as a read-only property set
    through ``set_paused``; here it is a plain attribute, because a test setting
    it directly is the point. ``call`` records and then either raises whatever
    ``raises`` holds or returns the reply registered for that method, defaulting
    to an empty object rather than None, since a viewer that answers a call
    always answers with something.
    """

    def __init__(self, replies=None, raises=None):
        self.paused = False
        self.calls = []
        self.replies = dict(replies or {})
        self.raises = raises

    async def call(self, method, params=None, *, timeout=None):
        self.calls.append((method, params or {}))
        if self.raises is not None:
            raise self.raises
        return self.replies.get(method, {})


@pytest.fixture
def project(tmp_path):
    (tmp_path / "cube.stl").write_bytes(_cube_stl())
    (tmp_path / "dimensions.json").write_text('{"width": 10}\n')
    return tmp_path


def tools_for(bus, serve_dir):
    """``{name: handler}`` for one built server, wrapper included."""
    return {t.name: t.handler for t in registry.MeshTools(bus, serve_dir).tools}


def text_of(result):
    """The text of a result's first text block, whatever else it carries."""
    for item in result["content"]:
        if item["type"] == "text":
            return item["text"]
    return ""


def payload_of(result):
    return json.loads(text_of(result))


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


async def test_the_three_grades_are_what_they_are_meant_to_be(project):
    assert registry.READ_CLASS == EXPECTED_READ_CLASS
    assert registry.VIEW_CLASS == EXPECTED_VIEW_CLASS
    assert registry.WRITE_CLASS == EXPECTED_WRITE_CLASS
    # The two derived sets, which are the ones the code actually acts on, and
    # which are different from each other on purpose.
    assert registry.GRADING.pre_allowed == EXPECTED_PRE_ALLOWED
    assert registry.GRADING.pause_gated == EXPECTED_PAUSE_GATED
    assert set(registry.GRADING.pre_allowed) != set(registry.GRADING.pause_gated)


async def test_every_classified_tool_exists_and_every_built_tool_is_classified(project):
    built = set(tools_for(FakeBus(), project))
    assert built == (
        set(EXPECTED_READ_CLASS) | set(EXPECTED_VIEW_CLASS) | set(EXPECTED_WRITE_CLASS)
    )


async def test_the_pre_allowed_names_are_exactly_what_the_session_pre_allows(project):
    """The two lists are one list, and this is the seam where a divergence
    would show up as a pre-allowed name matching nothing (fact 1): the tool
    server's ``pre_allowed`` is what annealage-agent's ``launch.py`` hands the Claude
    session as its allow list, namespaced under the server's own name."""
    tools = registry.MeshTools(FakeBus(), project)
    assert tools.pre_allowed == tuple(namespaced("mesh", name) for name in EXPECTED_PRE_ALLOWED)
    assert list(tools.mcp_servers) == ["mesh"]


# ---------------------------------------------------------------------------
# The viewer round trip
# ---------------------------------------------------------------------------


async def test_set_view_sends_exactly_what_it_was_given(project):
    bus = FakeBus(replies={"viewer.set_view": {"position": [1, 2, 3]}})
    result = await tools_for(bus, project)["set_view"](
        {"position": [1, 2, 3], "target": [0, 0, 0], "up_axis": "y"}
    )
    assert bus.calls == [
        (
            "viewer.set_view",
            {"position": [1.0, 2.0, 3.0], "target": [0.0, 0.0, 0.0], "up_axis": "y"},
        )
    ]
    assert payload_of(result) == {"position": [1, 2, 3]}
    assert "is_error" not in result


async def test_set_view_with_nothing_to_do_says_what_to_call_instead(project):
    bus = FakeBus()
    result = await tools_for(bus, project)["set_view"]({})
    assert result["is_error"] is True
    assert "get_view" in text_of(result) and "fit_view" in text_of(result)
    # Nothing was sent: an argument failure must not reach the browser as a
    # call whose params the command table would then have to defend against.
    assert bus.calls == []


async def test_a_two_number_point_names_the_field_that_was_wrong(project):
    result = await tools_for(FakeBus(), project)["set_view"]({"target": [1, 2]})
    assert result["is_error"] is True
    assert "target" in text_of(result)


async def test_capture_view_returns_a_real_image_block(project):
    """Fact 8: an ``image`` item in a tool result reaches the model as an
    image, so a capture needs no file write and no second round trip."""
    bus = FakeBus(
        replies={
            "viewer.capture_view": {
                "image": _png_data_url(),
                "width": 800,
                "height": 450,
                "format": "png",
                "camera": {"position": [1, 2, 3]},
            }
        }
    )
    result = await tools_for(bus, project)["capture_view"]({"width": 800})
    assert bus.calls == [("viewer.capture_view", {"width": 800})]
    images = [item for item in result["content"] if item["type"] == "image"]
    assert len(images) == 1
    assert images[0]["mimeType"] == "image/png"
    assert base64.b64decode(images[0]["data"])
    # And the numbers an image cannot carry.
    assert payload_of(result)["camera"] == {"position": [1, 2, 3]}


async def test_capture_view_says_so_rather_than_delivering_an_empty_image(project):
    bus = FakeBus(replies={"viewer.capture_view": {"image": "", "width": 0}})
    result = await tools_for(bus, project)["capture_view"]({})
    assert result["is_error"] is True
    assert "not delivered" in text_of(result)
    assert not [item for item in result["content"] if item["type"] == "image"]


# ---------------------------------------------------------------------------
# The pause gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", EXPECTED_PAUSE_GATED)
async def test_every_tool_that_changes_anything_refuses_while_paused(project, name):
    bus = FakeBus()
    bus.paused = True
    result = await tools_for(bus, project)[name](ARGS[name])
    assert result["is_error"] is True
    assert result == {
        "content": [{"type": "text", "text": registry.PAUSED_MESSAGE}],
        "is_error": True,
    }
    # Refused before anything ran, not after: a paused ``add_callout`` that had
    # already written the file would be a refusal in name only.
    assert bus.calls == []
    assert not (project / paths.CALLOUTS_JSON_NAME).exists()


@pytest.mark.parametrize("name", EXPECTED_READ_CLASS)
async def test_no_tool_that_changes_nothing_is_gated_by_pause(project, name):
    """Pausing exists so the human can work without the view moving. A model
    that keeps reading while paused does no harm and is better informed when
    the pause lifts, so none of these may refuse."""
    bus = FakeBus(
        replies={
            "viewer.capture_view": {
                "image": _png_data_url(),
                "width": 10,
                "height": 10,
                "format": "png",
            }
        }
    )
    bus.paused = True
    result = await tools_for(bus, project)[name](ARGS[name])
    assert "is_error" not in result, text_of(result)


async def test_the_paused_message_tells_the_model_what_to_do(project):
    """Fact 15: a refusal's text reaches the model verbatim, so this one is
    worth asserting on rather than leaving to read like a status code."""
    assert "read-only mesh tool" in registry.PAUSED_MESSAGE
    assert "ask them" in registry.PAUSED_MESSAGE


# ---------------------------------------------------------------------------
# Callouts: the file contract, written from the agent's side
# ---------------------------------------------------------------------------


async def test_add_callout_writes_the_documented_shape(project):
    result = await tools_for(FakeBus(), project)["add_callout"](
        {
            "point": [1, 2.5, 3],
            "comment": "  moved this wall out 2mm  ",
            "part": "bracket",
            "label": "+Y",
        }
    )
    assert "is_error" not in result
    written = json.loads((project / paths.CALLOUTS_JSON_NAME).read_text())
    assert written == {
        "annotations": [
            {
                "author": "agent",
                "point": [1.0, 2.5, 3.0],
                "comment": "moved this wall out 2mm",
                "part": "bracket",
                "label": "+Y",
                "id": 1,
            }
        ]
    }


async def test_add_callout_takes_the_next_id_past_the_highest_present(project):
    (project / paths.CALLOUTS_JSON_NAME).write_text(
        json.dumps({"annotations": [{"id": 4, "point": [0, 0, 0], "comment": "old"}]})
    )
    result = await tools_for(FakeBus(), project)["add_callout"](
        {"point": [0, 0, 1], "comment": "new"}
    )
    assert payload_of(result)["added"]["id"] == 5
    assert payload_of(result)["count"] == 2


async def test_add_callout_accepts_the_bare_array_shape_too(project):
    """The viewer accepts a bare array as well as ``{"annotations": [...]}``,
    so a callouts file a human or another agent left in that form must not be
    read as empty and overwritten."""
    (project / paths.CALLOUTS_JSON_NAME).write_text(
        json.dumps([{"id": 2, "point": [0, 0, 0], "comment": "theirs"}])
    )
    await tools_for(FakeBus(), project)["add_callout"]({"point": [0, 0, 1], "comment": "mine"})
    written = json.loads((project / paths.CALLOUTS_JSON_NAME).read_text())
    assert [a["comment"] for a in written["annotations"]] == ["theirs", "mine"]


async def test_add_callout_refuses_a_file_it_cannot_parse(project):
    """Appending would mean discarding whatever is in there, and a file that
    does not parse may be a file something else is mid-way through writing."""
    (project / paths.CALLOUTS_JSON_NAME).write_text("{ this is not json")
    result = await tools_for(FakeBus(), project)["add_callout"](
        {"point": [0, 0, 1], "comment": "mine"}
    )
    assert result["is_error"] is True
    assert "does not parse" in text_of(result)
    assert (project / paths.CALLOUTS_JSON_NAME).read_text() == "{ this is not json"


async def test_add_callout_refuses_a_symlinked_callouts_file(project, tmp_path):
    """A reviewed bundle can carry that name as a link to somewhere else, and
    writing through it would put the agent's callouts wherever it points."""
    outside = tmp_path.parent / "outside.json"
    outside.write_text("{}")
    (project / paths.CALLOUTS_JSON_NAME).symlink_to(outside)
    result = await tools_for(FakeBus(), project)["add_callout"](
        {"point": [0, 0, 1], "comment": "mine"}
    )
    assert result["is_error"] is True
    assert "not a plain, single-linked file" in text_of(result)
    assert outside.read_text() == "{}"


async def test_add_callout_needs_a_comment_worth_reading(project):
    result = await tools_for(FakeBus(), project)["add_callout"](
        {"point": [0, 0, 1], "comment": "   "}
    )
    assert result["is_error"] is True
    assert not (project / paths.CALLOUTS_JSON_NAME).exists()


async def test_delete_callout_removes_one_and_leaves_the_rest(project):
    (project / paths.CALLOUTS_JSON_NAME).write_text(
        json.dumps(
            {
                "annotations": [
                    {"id": 1, "point": [0, 0, 0], "comment": "keep"},
                    {"id": 2, "point": [0, 0, 1], "comment": "go"},
                ]
            }
        )
    )
    result = await tools_for(FakeBus(), project)["delete_callout"]({"id": 2})
    assert payload_of(result) == {
        "deleted": 2,
        "count": 1,
        "path": str(project / paths.CALLOUTS_JSON_NAME),
    }
    written = json.loads((project / paths.CALLOUTS_JSON_NAME).read_text())
    assert [a["id"] for a in written["annotations"]] == [1]


async def test_delete_callout_names_the_ids_that_do_exist(project):
    (project / paths.CALLOUTS_JSON_NAME).write_text(
        json.dumps({"annotations": [{"id": 7, "point": [0, 0, 0], "comment": "keep"}]})
    )
    result = await tools_for(FakeBus(), project)["delete_callout"]({"id": 2})
    assert result["is_error"] is True
    assert "the ids present are: 7" in text_of(result)


async def test_add_callout_stops_at_the_limit(project, monkeypatch):
    from annealage_mesh import review

    monkeypatch.setattr(review, "MAX_CALLOUTS", 2)
    (project / paths.CALLOUTS_JSON_NAME).write_text(
        json.dumps(
            {
                "annotations": [
                    {"id": 1, "point": [0, 0, 0], "comment": "a"},
                    {"id": 2, "point": [0, 0, 1], "comment": "b"},
                ]
            }
        )
    )
    result = await tools_for(FakeBus(), project)["add_callout"](
        {"point": [0, 0, 2], "comment": "c"}
    )
    assert result["is_error"] is True
    assert "delete_callout" in text_of(result)


# ---------------------------------------------------------------------------
# Comments: the human's side of the same contract, read only
# ---------------------------------------------------------------------------


async def test_list_comments_says_plainly_that_nothing_is_submitted_yet(project):
    result = await tools_for(FakeBus(), project)["list_comments"]({})
    assert "is_error" not in result
    assert "has not submitted any pin comments yet" in text_of(result)


async def test_list_comments_returns_the_submitted_record(project):
    (project / paths.COMMENTS_JSON_NAME).write_text(
        json.dumps(
            {
                "submitted_at": "2026-08-13T10:15:00+10:00",
                "count": 1,
                "annotations": [
                    {
                        "id": 1,
                        "part": "cube",
                        "label": "+Z",
                        "point": [1, 2, 3],
                        "comment": "too sharp",
                    }
                ],
            }
        )
    )
    payload = payload_of(await tools_for(FakeBus(), project)["list_comments"]({}))
    assert payload["count"] == 1
    assert payload["annotations"][0]["comment"] == "too sharp"
    assert payload["submitted_at"] == "2026-08-13T10:15:00+10:00"


async def test_list_callouts_reports_a_file_it_cannot_read_rather_than_zero(project):
    (project / paths.CALLOUTS_JSON_NAME).write_text("nonsense")
    result = await tools_for(FakeBus(), project)["list_callouts"]({})
    assert result["is_error"] is True
    assert "does not parse" in text_of(result)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


async def test_list_models_reports_what_the_manifest_would(project):
    (project / "sub").mkdir()
    (project / "sub" / "lid.stl").write_bytes(_cube_stl())
    (project / ".git").mkdir()
    (project / ".git" / "hidden.stl").write_bytes(_cube_stl())
    payload = payload_of(await tools_for(FakeBus(), project)["list_models"]({}))
    # The same recursive scan /manifest answers from, exclusions included: a
    # model the viewer cannot show must not be a rel the model is invited to
    # name, and one it can show must be listed.
    assert [m["rel"] for m in payload["models"]] == ["cube.stl", "sub/lid.stl"]
    assert payload["dir"] == str(project.resolve())
    assert payload["models"][0]["bytes"] == len(_cube_stl())


async def test_model_info_reports_geometry_from_the_packages_own_reader(project):
    payload = payload_of(await tools_for(FakeBus(), project)["model_info"]({"rel": "cube.stl"}))
    assert payload["format"] == "binary"
    assert payload["triangles"] == 2
    assert payload["extent"] == [10.0, 10.0, 0.0]
    assert payload["header"].startswith("mesh test cube")


async def test_model_info_names_the_models_that_do_exist(project):
    result = await tools_for(FakeBus(), project)["model_info"]({"rel": "nope.stl"})
    assert result["is_error"] is True
    assert "cube.stl" in text_of(result)


async def test_model_info_on_a_half_written_model_says_to_wait(project):
    (project / "half.stl").write_bytes(_cube_stl()[:60])
    result = await tools_for(FakeBus(), project)["model_info"]({"rel": "half.stl"})
    assert result["is_error"] is True
    assert "may still be being written" in text_of(result)


# ---------------------------------------------------------------------------
# measure, and snapshot's write path
# ---------------------------------------------------------------------------


async def test_measure_forwards_the_references_for_the_viewer_to_resolve(project):
    """The references are resolved in the browser, against the live pin list,
    which is what lets a pin the human has not submitted yet be measured."""
    bus = FakeBus(replies={"viewer.measure": {"distance": 12.5}})
    result = await tools_for(bus, project)["measure"]({"a": " pin:1 ", "b": "0,0,0"})
    assert bus.calls == [("viewer.measure", {"a": "pin:1", "b": "0,0,0"})]
    assert payload_of(result)["distance"] == 12.5


async def test_measure_quotes_the_forms_it_accepts(project):
    result = await tools_for(FakeBus(), project)["measure"]({"a": "", "b": "pin:2"})
    assert result["is_error"] is True
    assert "pin:3" in text_of(result) and "12.5,-3.2,44" in text_of(result)


async def test_snapshot_writes_the_captured_bytes_under_images(project):
    payload = b"\x89PNG\r\n\x1a\n" + b"pretend pixels"
    bus = FakeBus(
        replies={
            "viewer.capture_view": {
                "image": _png_data_url(payload),
                "width": 800,
                "height": 450,
                "format": "png",
            }
        }
    )
    result = await tools_for(bus, project)["snapshot"]({"name": "front-left"})
    payload_json = payload_of(result)
    written = project / files.IMAGES_DIRNAME / "front-left.png"
    assert written.read_bytes() == payload
    assert payload_json["path"] == str(written)
    assert payload_json["url"] == "/asset/front-left.png"


async def test_snapshot_names_the_file_for_the_format_the_browser_chose(project):
    """The browser re-encodes a capture that would not fit in one frame, so
    the extension follows the bytes rather than what was asked for."""
    bus = FakeBus(
        replies={
            "viewer.capture_view": {
                "image": "data:image/jpeg;base64," + base64.b64encode(b"jpeg bytes").decode(),
                "format": "jpeg",
                "width": 1568,
                "height": 880,
            }
        }
    )
    await tools_for(bus, project)["snapshot"]({"name": "wide.png"})
    assert (project / files.IMAGES_DIRNAME / "wide.jpg").is_file()


async def test_snapshot_never_overwrites_an_existing_image(project):
    bus = FakeBus(
        replies={"viewer.capture_view": {"image": _png_data_url(b"second"), "format": "png"}}
    )
    images = project / files.IMAGES_DIRNAME
    images.mkdir()
    (images / "front.png").write_bytes(b"first")
    await tools_for(bus, project)["snapshot"]({"name": "front"})
    assert (images / "front.png").read_bytes() == b"first"
    assert (images / "front-2.png").read_bytes() == b"second"


async def test_snapshot_refuses_a_symlinked_images_directory(project, tmp_path):
    """``images`` as a link makes this a writer into whatever it points at,
    which is a way to have this process create files anywhere its user can."""
    outside = tmp_path.parent / "elsewhere"
    outside.mkdir(exist_ok=True)
    (project / files.IMAGES_DIRNAME).symlink_to(outside, target_is_directory=True)
    bus = FakeBus(replies={"viewer.capture_view": {"image": _png_data_url(), "format": "png"}})
    result = await tools_for(bus, project)["snapshot"]({"name": "front"})
    assert result["is_error"] is True
    assert "must be a real" in text_of(result)
    assert list(outside.iterdir()) == []


async def test_snapshot_refuses_a_capture_it_cannot_decode(project):
    bus = FakeBus(replies={"viewer.capture_view": {"image": "data:image/png;base64,%%%%"}})
    result = await tools_for(bus, project)["snapshot"]({})
    assert result["is_error"] is True
    assert "nothing was written" in text_of(result)
    assert (
        not (project / files.IMAGES_DIRNAME).exists()
        or list((project / files.IMAGES_DIRNAME).iterdir()) == []
    )


async def test_snapshot_defaults_to_a_timestamped_name(project):
    bus = FakeBus(replies={"viewer.capture_view": {"image": _png_data_url(), "format": "png"}})
    result = await tools_for(bus, project)["snapshot"]({})
    name = os.path.basename(payload_of(result)["path"])
    assert name.startswith("snapshot-") and name.endswith(".png")


async def test_snapshot_says_to_choose_another_name_once_the_suffixes_run_out(project):
    """Distinguished from a containment refusal on purpose: one is "this name
    is not something mesh will write", the other is "pick a different one"."""
    from annealage_mesh.tools import review_tools

    images = project / files.IMAGES_DIRNAME
    images.mkdir()
    (images / "front.png").write_bytes(b"taken")
    for n in range(2, review_tools._SNAPSHOT_NAME_ATTEMPTS + 1):
        (images / ("front-%d.png" % n)).write_bytes(b"taken")

    bus = FakeBus(replies={"viewer.capture_view": {"image": _png_data_url(), "format": "png"}})
    result = await tools_for(bus, project)["snapshot"]({"name": "front"})
    assert result["is_error"] is True
    assert "pass a different name" in text_of(result)
