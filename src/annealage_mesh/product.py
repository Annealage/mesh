"""Mesh's description of itself to the agent layer, installed on import.

``agent/product.py`` defines the contract and what each field drives; this is
Mesh's answer to it. Importing this module installs ``MESH`` as the product
this process runs as, which is why every Mesh entry point (``cli.py``,
``app.py``) imports it before touching the agent layer, and why
``tests/conftest.py`` imports it too. Installing is idempotent, so the order
in which those imports happen does not matter.
"""

from . import __version__
from .agent import product as agent_product
from .agent.protocol import FrameSpec, object_error
from .agent.settings import USER, VIEWER_SECTION, Key
from .events import CalloutsChanged, ModelsChanged

_UP_AXIS_CHOICES = ("z", "y")

#: Mesh's one product setting: which axis the viewer treats as up. Read by the
#: page on load (``static/js/main.js`` hands ``initSettings`` the hook that
#: applies it), so its effect is ``load``; listed in the settings window's
#: Viewer section, before the generic tool-card preference.
UP_AXIS_KEY = Key(
    name="up_axis",
    type_name='"z" or "y"',
    default="z",
    layers=(USER,),
    effect="load",
    description="Which axis the viewer treats as up: z or y.",
    py_type=str,
    choices=_UP_AXIS_CHOICES,
    section=VIEWER_SECTION,
)


def _check_state(frame):
    return object_error(
        frame.get("state"), {"camera", "visibility", "selection", "mode"}, set(), "state.state"
    )


#: The page's report of its own view: camera, visibility, selection and mode.
#: The server keeps none of it; receiving one counts as interaction with that
#: tab (``agent/http/ws.py``).
STATE_FRAME = FrameSpec({"state"}, {"state"}, _check_state)


def _build_tools(bus, serve_dir, session_id):
    # Imported here, not at the top: the tool modules import the agent SDK,
    # which a viewer-only run must never pay for, and the agent layer only
    # calls this in agent mode.
    from .tools.registry import MeshTools

    return MeshTools(bus, serve_dir, session_id)


MESH = agent_product.Product(
    name="mesh",
    title="Mesh",
    display_name="Annealage Mesh",
    distribution="annealage-mesh",
    module="annealage_mesh",
    version=__version__,
    state_dirname=".mesh",
    config_dirname="annealage-mesh",
    mcp_server_name="mesh",
    viewer_only_command="annealage-mesh view",
    build_tools=_build_tools,
    settings_keys=(UP_AXIS_KEY,),
    events=(CalloutsChanged, ModelsChanged),
    inbound_frames={"state": STATE_FRAME},
    # The sketch overlay's composite (``static/js/sketch.js``), written as
    # images/sketch-<stamp>-<hex>.png beside the composer's own uploads.
    upload_kinds=("sketch",),
)

agent_product.install(MESH)
