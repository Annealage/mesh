"""Mesh's own event: the push that tells a page its models changed.

An ``AgentEvent`` subclass published through the same event log and broadcast
as every generic event, and registered with the agent layer as Mesh's product
event (``product.py``), which is what refuses a kind that would collide with
one the chat pane or the transcript export already interprets.

The other push the page reacts to, that the callouts changed, is the agent
layer's generic ``review_changed`` (``annealage_agent.review``), published by
its review watcher over Mesh's review store (``review.py``); it replaced
Mesh's own ``callouts_changed`` when the review model moved into the agent
layer, and the page handles it exactly as it handled that.
"""

from __future__ import annotations

import dataclasses
from typing import ClassVar, Optional

from annealage_agent.session.base import AgentEvent


@dataclasses.dataclass(frozen=True)
class ModelsChanged(AgentEvent):
    """The served directory's set of models, or one model's bytes, changed.

    This is what makes the tool a modelling loop rather than a review surface.
    An agent that edits a CAD source and regenerates an STL has changed the
    thing being discussed, and without this the viewer goes on showing the
    previous geometry until the human reopens the page, which is the one moment
    they are least likely to suspect the picture is stale.

    Carries no payload, for the same reason ``review_changed`` does not: the
    browser refetches ``/manifest`` and reloads geometry through the single
    writer of that state. Naming the changed files here would put the same facts
    on two paths, and the reload is cheap regardless, because ``/model`` answers
    a conditional request and an unchanged part costs a 304.
    """

    kind: ClassVar[str] = "models_changed"
    viewer: Optional[str] = None
