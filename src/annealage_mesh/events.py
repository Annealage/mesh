"""Mesh's own events: the two pushes that tell a page its served files changed.

Both are ``AgentEvent`` subclasses published through the same event log and
broadcast as every generic event, and both are registered with the agent layer
as Mesh's product events (``product.py``), which is what refuses a kind that
would collide with one the chat pane or the transcript export already
interprets. Neither carries a payload; each docstring says why.
"""

from __future__ import annotations

import dataclasses
from typing import ClassVar, Optional

from annealage_agent.session.base import AgentEvent


@dataclasses.dataclass(frozen=True)
class CalloutsChanged(AgentEvent):
    """The callouts watcher's push, replacing the browser's 1.5 s poll.

    Carries no payload: the browser refetches ``GET /callouts`` and hands
    the result to ``store.setCallouts``, the single writer of that state
    (M3's store contract). Putting the changed content in this event
    instead would give that state a second writer.
    """

    kind: ClassVar[str] = "callouts_changed"
    viewer: Optional[str] = None


@dataclasses.dataclass(frozen=True)
class ModelsChanged(AgentEvent):
    """The served directory's set of models, or one model's bytes, changed.

    This is what makes the tool a modelling loop rather than a review surface.
    An agent that edits a CAD source and regenerates an STL has changed the
    thing being discussed, and without this the viewer goes on showing the
    previous geometry until the human reopens the page, which is the one moment
    they are least likely to suspect the picture is stale.

    Carries no payload, for the same reason ``CalloutsChanged`` does not: the
    browser refetches ``/manifest`` and reloads geometry through the single
    writer of that state. Naming the changed files here would put the same facts
    on two paths, and the reload is cheap regardless, because ``/model`` answers
    a conditional request and an unchanged part costs a 304.
    """

    kind: ClassVar[str] = "models_changed"
    viewer: Optional[str] = None
