"""Mesh's own HTTP routes: the viewer page, its static assets, the models, and
the callouts and comments exchange files.

The helpers these share with the agent layer's routes (reading a JSON body,
streaming a file without blocking the event loop, serving through a cached
index, the token and Origin checks) live in ``agent/http``, which also holds
the ``/ws``, chat (including ``/upload`` and ``/asset``), settings, ``/mcp``
and ``/agent/static/`` routes every product has.
"""
