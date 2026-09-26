"""The mesh tool surface: what the model can do to the viewer and the project.

``registry.py`` grades every tool read, view or write and builds Mesh's tool
server from them; ``viewer_tools.py``, ``review_tools.py``, ``model_tools.py``
and ``cad_tools.py`` hold the handlers themselves, grouped by what they touch
rather than by whether they prompt. The four comment and callout tools in
``review_tools.py`` are the agent layer's shared review tools
(``annealage_agent.review.tools``) over Mesh's review store (``review.py``),
configured there to keep Mesh's names, descriptions, schemas and results.

What every tool shares is not Mesh's: the result shapes (``ok``/``fail``), the
model-visible naming, the pause gate and the failure mapping live in the agent
layer's ``annealage_agent/tools.py``, which the handler modules import directly.
"""
