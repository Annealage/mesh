"""The mesh tool surface: what the model can do to the viewer and the project.

``registry.py`` assembles the tools and owns the two policies every one of
them is subject to (the pause gate and the viewer-error mapping);
``viewer_tools.py``, ``review_tools.py`` and ``model_tools.py`` hold the
handlers themselves, grouped by what they touch rather than by whether they
prompt.

This module holds only what all four share: the server name that decides
every tool's model-visible name, and the two result shapes. Keeping them
here rather than in ``registry.py`` is what lets the handler modules import
them without importing the module that imports the handler modules.

Every handler returns one of ``ok`` or ``fail`` and nothing else. A tool
result reaches the model as text, so what a handler returns is prose it will
read: ``ok`` renders a payload as indented JSON, because coordinates and
part names are what these tools are for and JSON is the shape a model reads
them out of most reliably, and ``fail`` returns a sentence saying what went
wrong and what to do instead. That second half matters more than it looks:
a deny's message reaches the model verbatim (plan section 2a, fact 15), so
"no viewer connected; ask the human to open <url>" is worth more than a
status code.
"""

import json

#: The MCP server name, which fixes every tool's model-visible name as
#: ``mcp__mesh__<tool>``. A bare name in an allow list, a deny list or a hook
#: matcher silently matches nothing (plan section 2, fact 1), so nothing in
#: this package writes one by hand: it goes through ``namespaced`` below.
MESH_SERVER_NAME = "mesh"


def namespaced(name):
    """The model-visible name of one mesh tool."""
    return "mcp__%s__%s" % (MESH_SERVER_NAME, name)


def ok(payload=None, *, text=None):
    """A successful tool result: ``payload`` as JSON, or ``text`` verbatim."""
    if text is None:
        text = json.dumps(payload, indent=2, default=str)
    return {"content": [{"type": "text", "text": text}]}


def fail(message):
    """A failed tool result, whose ``message`` the model reads as the reason.

    ``is_error`` is what the SDK turns into a tool result the model is told
    failed; without it a refusal reads as a successful call that happened to
    return the word "refused", which a model will act on as if it had worked.
    """
    return {"content": [{"type": "text", "text": message}], "is_error": True}
