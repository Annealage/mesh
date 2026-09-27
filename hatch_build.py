"""Bundle the agent package into Mesh's wheel.

annealage-agent has no release on any index, and a published wheel cannot
depend on a path, so a Mesh wheel carries the agent itself: the
``annealage_agent`` package from the ``lib/agent`` submodule, at the commit its
gitlink pins, as a second top-level package beside ``annealage_mesh``. Its
runtime dependencies are declared in Mesh's own ``dependencies``
(pyproject.toml), and Mesh's published metadata names no annealage-agent at
all.

Only a standard wheel bundles it. An editable install (a development checkout)
gets the agent from the ``agent`` dependency group instead, installed editable
from ``lib/agent``; a copy in site-packages would shadow that and go stale on
the next change to ``lib/agent``.

The sdist carries ``lib/agent/src/annealage_agent`` (its ``include`` list), so
a wheel built from it, which is what ``uv build`` does, finds it here too.
"""

import os

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

AGENT_PACKAGE = os.path.join("lib", "agent", "src", "annealage_agent")


class BundleAgentHook(BuildHookInterface):
    def initialize(self, version, build_data):
        if self.target_name != "wheel" or version != "standard":
            return
        source = os.path.join(self.root, AGENT_PACKAGE)
        if not os.path.isfile(os.path.join(source, "__init__.py")):
            raise FileNotFoundError(
                "%s is missing, so the wheel would ship without the agent package: "
                "run `git submodule update --init lib/agent` first" % AGENT_PACKAGE
            )
        build_data["force_include"][source] = "annealage_agent"
