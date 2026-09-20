"""Assembly of the mesh tool server, and the two policies every tool obeys.

Seventeen flat tools on one in-process MCP server, declared with the SDK's
``@tool`` decorator and registered through ``create_sdk_mcp_server``. Flat
rather than a ``search_actions``/``execute_action`` pair, because the CLI
already surfaces tools through its own deferred tool search, so a second
discovery layer would duplicate it. That makes each tool's **description** the
search surface, which is why the descriptions in the three handler modules are
written as sentences about when to reach for the tool rather than as labels.
Revisit tiering only past roughly twenty tools.

The classification below is the whole permission design for these tools, and it
sorts them by **what a mistake would cost**, in three grades rather than two.

``READ_CLASS`` changes nothing. Reading the camera, the part list, the
comments, a model's geometry or a screenshot leaves the project and the view
exactly as they were, so these are pre-allowed and never interrupt anyone.

``VIEW_CLASS`` changes only what is on the screen, and does so in front of the
human, who is looking at that screen: the camera, which part is shown, which
axis is up, which pin is selected. These are pre-allowed too. The reasoning is
not that they are harmless in the abstract, it is that an approval card is the
wrong control for them: the loop this tool exists for has the model reframing a
part it has just regenerated, several times a turn, and a card per camera move
would either be clicked without reading or turned off with one standing grant,
which is worse than not asking. The control that fits is the pause switch,
which refuses all of them at once for as long as the human wants the view to
hold still, and that is what it is for.

``WRITE_CLASS`` leaves something behind after the page is closed: a callout in
a file the human's own tooling reads, an image in the project directory, or a
transcript that carries whatever was said about the hardware under review.
These are deliberately **absent** from every allow list, which is what makes
them reach the broker and therefore the human as a card. Adding one of these
names to a pre-allowed list anywhere would silently remove that card.

So two derived sets follow, and they are not the same set:
``PRE_ALLOWED`` is read plus view, and ``PAUSE_GATED`` is view plus write. The
three tuples here are the only place any of this is written down, and
``_verify`` refuses to build a server whose tools do not match them exactly, so
a tool added to a handler module without being classified fails at startup
rather than defaulting into a posture nobody chose.

The tuples are ordered as plan section 3.9 lists them, so the allow list this
module produces can be read against the plan line by line.
"""

import asyncio
import dataclasses
import sys
from collections import namedtuple

from claude_agent_sdk import create_sdk_mcp_server

from .. import __version__
from ..viewers import CallError, NoViewerConnected, ViewerGone
from . import MESH_SERVER_NAME, cad_tools, fail, model_tools, namespaced, review_tools, viewer_tools

#: Changes nothing. Pre-allowed, and not gated by the pause switch.
READ_CLASS = (
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

#: Changes what is on screen and nothing else. Pre-allowed, because a card per
#: camera move is the wrong control for something the human is watching happen;
#: gated by the pause switch, which is the right one.
VIEW_CLASS = (
    "set_view",
    "fit_view",
    "set_visibility",
    "set_up_axis",
    "select_pin",
)

#: Leaves something on disk. Reaches the broker, and therefore the human, and is
#: gated by the pause switch as well.
WRITE_CLASS = (
    "add_callout",
    "delete_callout",
    "snapshot",
    "export_transcript",
)

#: What never prompts, as the model sees it, which is what goes into
#: ``allowed_tools``. ``session/sdk.py`` re-exports this under its own name; it
#: is computed here, beside the classification, so there is one list.
PRE_ALLOWED = READ_CLASS + VIEW_CLASS
PRE_ALLOWED_MESH_TOOLS = tuple(namespaced(name) for name in PRE_ALLOWED)

#: What the pause switch refuses: everything that changes anything, whether the
#: change is to the screen or to the project. Deliberately not the same set as
#: what prompts, because the two questions are different: a card asks "may this
#: happen at all", and the pause switch says "not right now, I am working".
PAUSE_GATED = VIEW_CLASS + WRITE_CLASS

# What a gated tool says while the human has viewer control paused. It is
# written for the model to act on, not merely to log: a deny's message reaches
# it verbatim (plan section 2a, fact 15), so this says what is still possible
# and what would lift the refusal, rather than only that something was refused.
PAUSED_MESSAGE = (
    "Refused: the human has paused viewer control, so nothing may change the "
    "view or the callouts right now. They are most likely lining up a view or "
    "editing a pin comment and do not want the model moving underneath them. "
    "Every read-only mesh tool still works, so keep looking if that helps; "
    "otherwise say what you were about to do and ask them to press Paused in "
    "the viewer's topbar when they are ready."
)


#: One already-``_wrap``-gated tool, in the shape a non-Claude MCP bridge
#: (``http/routes_mcp.py``) needs to build its own server from -
#: ``tool_table()``'s values, below. ``write`` folds in ``WRITE_CLASS``
#: membership so that module can gate a write-class call through
#: ``PermissionBroker`` (Codex's app-server has no equivalent of Claude's own
#: ``can_use_tool`` hook for a generic MCP tool call - see that module's
#: docstring) without importing this one at its own top level, which would
#: pull ``claude_agent_sdk`` into every viewer-only run.
ToolSpec = namedtuple("ToolSpec", "schema description handler write")

#: JSON Schema type words for the plain ``{param: python_type}`` shorthand a
#: handful of this package's own tools use (``viewer_tools.py``'s
#: ``set_visibility``, ``set_up_axis``, ``select_pin``, ``measure``); every
#: python type any of them actually uses is a key here, and anything else
#: falls back to ``"string"``, matching
#: ``claude_agent_sdk``'s own private ``_python_type_to_json_schema``'s final
#: fallback for a type it does not recognise either.
_JSON_SCHEMA_TYPES = {str: "string", int: "integer", float: "number", bool: "boolean"}


def _tool_json_schema(input_schema):
    """The JSON Schema ``inputSchema`` a non-Claude MCP transport needs for
    one tool, from the exact ``input_schema`` its own ``@tool(...)`` call
    declared.

    Every tool in this package declares one of two shapes: already a full
    JSON Schema object (has both ``"type"`` and ``"properties"``), or the
    ``{param: python_type}`` shorthand ``claude_agent_sdk``'s own ``@tool``
    also accepts and expands itself, only for Claude
    (``create_sdk_mcp_server``'s private ``_build_schema``, not reused here:
    that function is unstable, single-underscore SDK-internal API, and this
    mirrors only the two shapes this package's own tools actually declare,
    never that function's third, TypedDict, branch, which nothing here
    uses). ``{}`` - every no-argument tool's declared schema - falls into
    the second branch and comes out as an object schema with no properties,
    the same shape ``create_sdk_mcp_server`` would build for it.
    """
    if "type" in input_schema and "properties" in input_schema:
        return input_schema
    properties = {
        name: {"type": _JSON_SCHEMA_TYPES.get(py_type, "string")}
        for name, py_type in input_schema.items()
    }
    return {"type": "object", "properties": properties, "required": list(properties)}


def _verify(tools):
    """Refuse a tool set that does not match the classification above."""
    built = [t.name for t in tools]
    duplicated = sorted({n for n in built if built.count(n) > 1})
    if duplicated:
        raise RuntimeError("mesh tools declared twice: %s" % ", ".join(duplicated))
    grades = {"read": READ_CLASS, "view": VIEW_CLASS, "write": WRITE_CLASS}
    for first, second in (("read", "view"), ("read", "write"), ("view", "write")):
        overlap = sorted(set(grades[first]) & set(grades[second]))
        if overlap:
            raise RuntimeError(
                "mesh tool(s) %s are classified both %s and %s"
                % (", ".join(overlap), first, second)
            )
    classified = set(READ_CLASS) | set(VIEW_CLASS) | set(WRITE_CLASS)
    unclassified = sorted(set(built) - classified)
    if unclassified:
        raise RuntimeError(
            "mesh tool(s) %s are built but not classified read, view or write in "
            "tools/registry.py; every default is wrong for something, so there is "
            "no default" % ", ".join(unclassified)
        )
    missing = sorted(classified - set(built))
    if missing:
        raise RuntimeError(
            "mesh tool(s) %s are classified in tools/registry.py but not built, "
            "so their names are pre-allowed and match nothing" % ", ".join(missing)
        )


def _wrap(tool_def, *, bus, gated):
    """Apply the pause gate and the failure mapping to one tool.

    Both live here rather than in each handler, which is what lets the handler
    modules be plain argument-validate-call-return code with no ``try`` blocks
    of their own. The mapping is not cosmetic: the four ways a viewer call can
    fail mean four different things to a model, and flattening them to one
    message would leave it retrying a call that will never work or giving up on
    one that would work on the next attempt.

    ``ValueError`` is the handler modules' way of rejecting an argument, and
    its message is written for the model, so it is passed through verbatim.
    Anything else reaching here is a bug in this package: it is logged for the
    human with the tool's name and reported as a failed call, rather than
    raised into the MCP layer, where it would reach the model as an
    infrastructure error that says nothing about which tool broke.
    """

    async def handler(args):
        if gated and bus.paused:
            return fail(PAUSED_MESSAGE)
        try:
            return await tool_def.handler(args)
        except ValueError as exc:
            return fail(str(exc))
        except NoViewerConnected as exc:
            # Carries plan section 3.3's exact wording, including the URL to
            # ask the human to open, so it is passed through unedited.
            return fail(str(exc))
        except ViewerGone:
            return fail(
                "the view this went to closed before it answered, so %s "
                "did not happen; ask the human whether the page is still "
                "open, then try again" % tool_def.name
            )
        except asyncio.TimeoutError:
            return fail(
                "the viewer did not answer %s in time, so it may or may "
                "not have happened; the page may be busy or in a "
                "background tab. Read the state back before assuming "
                "either way" % tool_def.name
            )
        except CallError as exc:
            error = exc.error or {}
            return fail(
                "the viewer refused %s: %s (%s)"
                % (
                    tool_def.name,
                    error.get("message", "no reason given"),
                    error.get("code", "no code"),
                )
            )
        except Exception as exc:
            sys.stderr.write("error: mesh tool %s failed: %r\n" % (tool_def.name, exc))
            return fail(
                "%s failed inside mesh itself (%s), which is a bug rather "
                "than anything you did; tell the human and carry on "
                "without it" % (tool_def.name, type(exc).__name__)
            )

    return dataclasses.replace(tool_def, handler=handler)


class MeshTools:
    """The mesh tool server for one session.

    Built per session rather than at import, because every handler closes over
    the ``ViewerBus`` and the served directory of the run it belongs to.

    ``session_id`` names the conversation, for the one tool that writes it out.

    ``mcp_servers`` is the mapping ``ClaudeAgentOptions`` takes. Its key and
    the server's own name are the same constant, deliberately: the key is what
    the model-visible ``mcp__<key>__<tool>`` name is built from, so a key that
    disagreed with ``MESH_SERVER_NAME`` would leave every pre-allowed name
    matching nothing and every one of those tools prompting.
    """

    def __init__(self, bus, serve_dir, session_id=None):
        self.bus = bus
        self.serve_dir = serve_dir
        self.session_id = session_id
        built = (
            model_tools.build(serve_dir)
            + viewer_tools.build(bus)
            + review_tools.build(bus, serve_dir, session_id)
            + cad_tools.build(serve_dir)
        )
        _verify(built)
        self.tools = tuple(
            _wrap(tool_def, bus=bus, gated=tool_def.name in PAUSE_GATED) for tool_def in built
        )
        self.server = create_sdk_mcp_server(
            MESH_SERVER_NAME, version=__version__, tools=list(self.tools)
        )

    @property
    def mcp_servers(self):
        return {MESH_SERVER_NAME: self.server}

    def tool_table(self):
        """``{name: ToolSpec(schema, description, handler, write)}`` off the
        same already-``_wrap``-gated handlers ``.mcp_servers`` is built from
        - the transport-neutral shape a non-Claude driver's own MCP bridge
        (``http/routes_mcp.py``) builds its own server from, without
        duplicating the pause-gate/failure-mapping ``_wrap`` already applied
        above. Description travels alongside the schema, not only the name:
        it is the search surface a model picks a tool from (this module's own
        docstring), so a transport that dropped it would leave a non-Claude
        backend calling these tools blind to what each one is for.
        """
        return {
            tool_def.name: ToolSpec(
                schema=_tool_json_schema(tool_def.input_schema),
                description=tool_def.description,
                handler=tool_def.handler,
                write=tool_def.name in WRITE_CLASS,
            )
            for tool_def in self.tools
        }
