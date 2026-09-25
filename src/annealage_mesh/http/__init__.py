"""Mesh's own HTTP routes: the viewer page, its static assets, the models, the
callouts and comments exchange files, and ``/asset``.

The helpers these share with the agent layer's routes (reading a JSON body,
streaming a file without blocking the event loop, the token and Origin
checks) live in ``agent/http``, which also holds the ``/ws``, chat, settings
and ``/mcp`` routes every product has.
"""
