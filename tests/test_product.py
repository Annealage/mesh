"""Tests for Mesh's answer to the agent layer's Product contract
(``annealage_mesh/product.py``).

The contract itself (identity and registrations read from whatever product is
installed, and from nowhere else) is tested in annealage-agent's suite, against
a toy product. What is Mesh's is that importing ``annealage_mesh.product``
installs ``MESH`` and what ``MESH`` registers.
"""

import subprocess
import sys

from annealage_agent import product, protocol, settings
from annealage_agent.http import routes_chat
from annealage_agent.session import base

from annealage_mesh.events import CalloutsChanged, ModelsChanged
from annealage_mesh.product import MESH, UP_AXIS_KEY


def test_importing_the_product_module_installs_mesh():
    """In a fresh interpreter, so nothing this suite installed can stand in
    for the import doing it."""
    script = (
        "import annealage_mesh.product\n"
        "from annealage_agent import product\n"
        "print(product.current().name)\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "mesh\n"


def test_mesh_registers_its_setting_frame_upload_kind_and_events():
    assert product.current() is MESH
    assert settings.KEYS_BY_NAME["up_axis"] is UP_AXIS_KEY
    assert protocol.is_product_frame("state")
    assert routes_chat.upload_kinds() == ("upload", "sketch")
    assert base.PRODUCT_EVENTS == (CalloutsChanged, ModelsChanged)
    assert [event.kind for event in base.PRODUCT_EVENTS] == ["callouts_changed", "models_changed"]
