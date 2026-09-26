"""Mesh's tools, graded: the one place their permission posture is decided.

Seventeen flat tools on one in-process MCP server, declared with the SDK's
``@tool`` decorator in the four handler modules and assembled here into the
agent layer's ``ToolServer``. Flat rather than a
``search_actions``/``execute_action`` pair, because the CLI already surfaces
tools through its own deferred tool search, so a second discovery layer would
duplicate it. That makes each tool's **description** the search surface, which
is why the descriptions in the handler modules are written as sentences about
when to reach for the tool rather than as labels. Revisit tiering only past
roughly twenty tools.

The three tuples below are the whole permission design for these tools: what
each grade means, and why view is pre-allowed but pause-gated, is written down
once, in ``annealage_agent/tools.py``. The agent layer refuses to build a server whose
tools do not match these tuples exactly, so a tool added to a handler module
without being classified here fails at startup rather than defaulting into a
posture nobody chose.

The tuples are ordered as plan section 3.9 lists them, so the allow list the
agent layer derives from them can be read against the plan line by line.
"""

from annealage_agent.tools import Grading, ToolServer

from . import cad_tools, model_tools, review_tools, viewer_tools

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
    "mesh_dimensions_set",
)

#: The three grades together, which is what the agent layer builds the server,
#: the pre-allowed list and the pause gate from (``annealage_agent/tools.py``). The
#: derived sets are ``GRADING.pre_allowed`` (read plus view) and
#: ``GRADING.pause_gated`` (view plus write).
GRADING = Grading(read=READ_CLASS, view=VIEW_CLASS, write=WRITE_CLASS)

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


class MeshTools(ToolServer):
    """Mesh's tool server for one session: the product's ``build_tools``.

    Built per session rather than at import, because every handler closes over
    the ``ViewerBus`` and the served directory of the run it belongs to.
    ``session_id`` names the conversation, for the one tool that writes it out.
    ``review_store`` is the app's ``MeshFilesStore`` (``bus.review_store``);
    ``None`` builds one over ``serve_dir``, which is all a test needs.
    """

    def __init__(self, bus, serve_dir, session_id=None, review_store=None):
        self.bus = bus
        self.serve_dir = serve_dir
        self.session_id = session_id
        built = (
            model_tools.build(serve_dir)
            + viewer_tools.build(bus)
            + review_tools.build(bus, serve_dir, session_id, store=review_store)
            + cad_tools.build(serve_dir)
        )
        super().__init__(built, grading=GRADING, bus=bus, paused_message=PAUSED_MESSAGE)
