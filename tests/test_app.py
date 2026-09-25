"""Mesh's own page under the agent layer's Content-Security-Policy.

How the policy is computed (hashing a page's inline scripts at startup,
failing closed on a missing page, the directives every response carries) is
the agent layer's, and is tested in that package's suite against its toy
page. What is Mesh's is its viewer page: that ``viewer.html`` has exactly one
inline script, the import map, and that the policy built from it names that
script by hash rather than allowing inline script by category.
"""

import pytest
from annealage_agent import app as agent_app

from annealage_mesh.http.routes_viewer import VIEWER_HTML

pytestmark = pytest.mark.asyncio


async def test_the_policy_names_the_import_map_by_hash_not_by_unsafe_inline():
    """The page has exactly one inline script, so it is allowed by content
    rather than by category. 'unsafe-inline' would also allow any script an
    injection managed to place in the markup."""
    policy = agent_app.content_security_policy(VIEWER_HTML)
    hashes = agent_app.inline_script_hashes(VIEWER_HTML)
    assert len(hashes) == 1
    assert hashes[0] in policy
    assert "unsafe-inline" not in policy
    assert "unsafe-eval" not in policy
