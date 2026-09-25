"""Tests for Mesh's own inbound frame, ``state``, as ``product.py`` registers it.

How a product frame is validated (required and allowed top-level keys, the
product's check, the reasons given) is the agent layer's, and tested in
annealage-agent's own suite. What stays here is Mesh's declaration: the page's
report of its view carries ``camera``, ``visibility``, ``selection`` and
``mode``, under exactly those names, and nothing else.
"""

import pytest
from annealage_agent.protocol import PROTOCOL_VERSION, validate_inbound


@pytest.mark.parametrize(
    "frame",
    [
        {"v": PROTOCOL_VERSION, "type": "state", "state": {}},
        {
            "v": PROTOCOL_VERSION,
            "type": "state",
            "state": {
                "camera": {},
                "visibility": {"lid": True},
                "selection": 3,
                "mode": "annotate",
            },
        },
    ],
)
def test_validate_inbound_accepts_a_well_shaped_state_frame(frame):
    ok, result = validate_inbound(frame)
    assert ok is True
    assert result == frame


def test_validate_inbound_rejects_state_with_an_unknown_key():
    ok, reason = validate_inbound({"v": PROTOCOL_VERSION, "type": "state", "state": {"zoom": 2}})
    assert ok is False
    assert "zoom" in reason
