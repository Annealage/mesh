"""Assembly of the mesh tool server, and the two policies every tool obeys.

Sixteen flat tools on one in-process MCP server, declared with the SDK's
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
a file the human's own tooling reads, or an image in the project directory.
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

from claude_agent_sdk import create_sdk_mcp_server

from .. import __version__
from ..viewers import CallError, NoViewerConnected, ViewerGone
from . import MESH_SERVER_NAME, fail, namespaced
from . import model_tools, review_tools, viewer_tools

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
    "the viewer's topbar when they are ready.")


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
            raise RuntimeError("mesh tool(s) %s are classified both %s and %s"
                               % (", ".join(overlap), first, second))
    classified = set(READ_CLASS) | set(VIEW_CLASS) | set(WRITE_CLASS)
    unclassified = sorted(set(built) - classified)
    if unclassified:
        raise RuntimeError(
            "mesh tool(s) %s are built but not classified read, view or write in "
            "tools/registry.py; every default is wrong for something, so there is "
            "no default" % ", ".join(unclassified))
    missing = sorted(classified - set(built))
    if missing:
        raise RuntimeError(
            "mesh tool(s) %s are classified in tools/registry.py but not built, "
            "so their names are pre-allowed and match nothing"
            % ", ".join(missing))


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
            return fail("the view this went to closed before it answered, so %s "
                        "did not happen; ask the human whether the page is still "
                        "open, then try again" % tool_def.name)
        except asyncio.TimeoutError:
            return fail("the viewer did not answer %s in time, so it may or may "
                        "not have happened; the page may be busy or in a "
                        "background tab. Read the state back before assuming "
                        "either way" % tool_def.name)
        except CallError as exc:
            error = exc.error or {}
            return fail("the viewer refused %s: %s (%s)"
                        % (tool_def.name, error.get("message", "no reason given"),
                           error.get("code", "no code")))
        except Exception as exc:
            sys.stderr.write("error: mesh tool %s failed: %r\n" % (tool_def.name, exc))
            return fail("%s failed inside mesh itself (%s), which is a bug rather "
                        "than anything you did; tell the human and carry on "
                        "without it" % (tool_def.name, type(exc).__name__))

    return dataclasses.replace(tool_def, handler=handler)


class MeshTools:
    """The mesh tool server for one session.

    Built per session rather than at import, because every handler closes over
    the ``ViewerBus`` and the served directory of the run it belongs to.

    ``mcp_servers`` is the mapping ``ClaudeAgentOptions`` takes. Its key and
    the server's own name are the same constant, deliberately: the key is what
    the model-visible ``mcp__<key>__<tool>`` name is built from, so a key that
    disagreed with ``MESH_SERVER_NAME`` would leave every pre-allowed name
    matching nothing and every one of those tools prompting.
    """

    def __init__(self, bus, serve_dir):
        self.bus = bus
        self.serve_dir = serve_dir
        built = (model_tools.build(serve_dir)
                 + viewer_tools.build(bus)
                 + review_tools.build(bus, serve_dir))
        _verify(built)
        self.tools = tuple(
            _wrap(tool_def, bus=bus, gated=tool_def.name in PAUSE_GATED)
            for tool_def in built)
        self.server = create_sdk_mcp_server(
            MESH_SERVER_NAME, version=__version__, tools=list(self.tools))

    @property
    def mcp_servers(self):
        return {MESH_SERVER_NAME: self.server}
