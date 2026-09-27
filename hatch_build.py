"""Bundle the agent package into Mesh's wheel.

annealage-agent is not published on its own, and a published wheel cannot
depend on a path, so a Mesh wheel carries the agent itself: the
``annealage_agent`` package from the ``lib/agent`` submodule, at the commit its
gitlink pins, as a second top-level package beside ``annealage_mesh``. Its
runtime dependencies are declared in Mesh's own ``dependencies``
(pyproject.toml), and Mesh's published metadata names no annealage-agent at
all.

That copy of the agent's dependencies is checked here, since nothing else
would notice it falling behind (a checkout installs the agent editable, with
its own dependencies): a standard wheel refuses to build unless Mesh declares
every requirement in ``lib/agent/pyproject.toml``'s ``dependencies`` and its
``codex`` extra, spelled the same. ``tools/bump-agent.sh`` runs the same check
(``python hatch_build.py``) when it moves ``lib/agent``.

Only a standard wheel bundles the agent. An editable install (a development
checkout) gets it from the ``agent`` dependency group instead, installed
editable from ``lib/agent``; a copy in site-packages would shadow that and go
stale on the next change to ``lib/agent``.

The sdist carries ``lib/agent/src/annealage_agent`` and
``lib/agent/pyproject.toml`` (its ``include`` list), so a wheel built from it,
which is what ``uv build`` does, finds both here too.
"""

import os
import sys

from hatchling.builders.hooks.plugin.interface import BuildHookInterface
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

AGENT_DIR = os.path.join("lib", "agent")
AGENT_PACKAGE = os.path.join(AGENT_DIR, "src", "annealage_agent")
AGENT_PYPROJECT = os.path.join(AGENT_DIR, "pyproject.toml")


def _normal(requirement):
    """One spelling of a requirement: its canonical name, then packaging's
    rendering of the extras, specifiers and marker."""
    parsed = Requirement(requirement)
    parsed.name = canonicalize_name(parsed.name)
    return str(parsed)


def missing_agent_requirements(root, dependencies, codex):
    """What ``lib/agent/pyproject.toml`` requires that Mesh doesn't declare,
    one ``"<requirement> (<where>)"`` line each: its dependencies against
    ``dependencies``, its codex extra against ``codex``."""
    with open(os.path.join(root, AGENT_PYPROJECT), "rb") as fh:
        agent = tomllib.load(fh)["project"]
    pairs = [
        (agent.get("dependencies", []), dependencies, "dependencies"),
        (agent.get("optional-dependencies", {}).get("codex", []), codex, "the codex extra"),
    ]
    missing = []
    for required, declared, where in pairs:
        have = {_normal(r) for r in declared}
        missing.extend("%s (%s)" % (r, where) for r in required if _normal(r) not in have)
    return missing


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
        core = self.metadata.core
        missing = missing_agent_requirements(
            self.root, core.dependencies, core.optional_dependencies.get("codex", [])
        )
        if missing:
            raise ValueError(
                "the bundled agent (lib/agent) requires what Mesh's pyproject.toml does not "
                "declare, so the wheel would install without it; copy these into Mesh's "
                "dependencies or codex extra:\n  %s" % "\n  ".join(missing)
            )
        build_data["force_include"][source] = "annealage_agent"


if __name__ == "__main__":
    # tools/bump-agent.sh: the same check against pyproject.toml as it stands.
    # Exit 3 when something is missing, so a failure to run at all is told apart.
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "pyproject.toml"), "rb") as fh:
        mesh = tomllib.load(fh)["project"]
    lines = missing_agent_requirements(
        here,
        mesh.get("dependencies", []),
        mesh.get("optional-dependencies", {}).get("codex", []),
    )
    for line in lines:
        print(line)
    sys.exit(3 if lines else 0)
