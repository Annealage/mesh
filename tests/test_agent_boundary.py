"""The agent layer's boundary: nothing under ``annealage_mesh/agent/`` depends
on Mesh.

``agent/`` is the part of this package that is to become ``annealage-agent``,
shared with other products, so it may import itself, the standard library and
third-party packages, and nothing else of ``annealage_mesh``: what it needs to
know about the product it runs in comes through ``agent/product.py``. This is
checked by reading the source rather than by importing it, because an import
that only happens inside a function (the way the backends' session modules are
imported) is as much a dependency as one at the top of a file, and importing
would not see it until that function ran.

Two things are refused:

- an import of an ``annealage_mesh`` module outside ``annealage_mesh.agent``,
  with relative imports resolved against the importing module's own package
  (``from ... import paths`` in ``agent/session/sdk.py`` is
  ``annealage_mesh.paths``), and a string naming such a module, which is how a
  module path handed to a subprocess would re-couple the two: any string that
  starts with a module path outside the agent layer, including the
  ``module:attr`` entry-point form and the ``"annealage_mesh."`` prefix of a
  path built by concatenation;
- any reference to the 3D domain (``stl``, ``cad``, ``cadquery``, ``trimesh``,
  ``three``): an imported module name, an identifier (as one ``snake_case``
  word of it), or a string constant that is not a docstring. Docstrings and
  comments are prose and may still say where an idea came from.

The last tests feed the checker regressions it must catch, so it cannot pass
by checking nothing.
"""

import ast
import re
from pathlib import Path

import pytest

AGENT_DIR = Path(__file__).resolve().parents[1] / "src" / "annealage_mesh" / "agent"
ROOT_PACKAGE = "annealage_mesh"
AGENT_PACKAGE = "annealage_mesh.agent"
DOMAIN_WORDS = frozenset({"stl", "cad", "cadquery", "trimesh", "three"})
_DOMAIN_TEXT_RE = re.compile(r"\b(stl|cad|cadquery|trimesh|three)\b", re.IGNORECASE)
_MODULE_TEXT_RE = re.compile(r"^annealage_mesh(\.[A-Za-z_][A-Za-z0-9_]*)*\.?(:|$)")


def _agent_modules():
    """``(path, dotted module name, is_package)`` for every module under agent/."""
    for path in sorted(AGENT_DIR.rglob("*.py")):
        parts = path.relative_to(AGENT_DIR).with_suffix("").parts
        is_package = parts[-1] == "__init__"
        if is_package:
            parts = parts[:-1]
        yield path, ".".join((AGENT_PACKAGE,) + parts), is_package


def _imported_names(node, module_name, is_package):
    """Every absolute module name ``node`` may import. For ``from X import a``
    that is ``X`` and ``X.a``, because ``a`` may itself be a submodule
    (``from .. import paths``)."""
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if node.level == 0:
        base = node.module
    else:
        package = module_name if is_package else module_name.rpartition(".")[0]
        for _ in range(node.level - 1):
            package = package.rpartition(".")[0]
        base = package + ("." + node.module if node.module else "")
    return [base] + ["%s.%s" % (base, alias.name) for alias in node.names if alias.name != "*"]


def _outside_agent(name):
    return (name == ROOT_PACKAGE or name.startswith(ROOT_PACKAGE + ".")) and not (
        name == AGENT_PACKAGE or name.startswith(AGENT_PACKAGE + ".")
    )


def _module_of(text):
    """The module a string names: the part before an entry point's ``:attr``,
    without the trailing dot a string built by concatenation
    (``"annealage_mesh." + name``) starts with."""
    return text.partition(":")[0].rstrip(".")


def _docstring_nodes(tree):
    nodes = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                if isinstance(body[0].value.value, str):
                    nodes.add(id(body[0].value))
    return nodes


def _identifiers(node):
    if isinstance(node, ast.Name):
        yield node.id
    elif isinstance(node, ast.Attribute):
        yield node.attr
    elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        yield node.name
    elif isinstance(node, ast.arg):
        yield node.arg
    elif isinstance(node, ast.keyword) and node.arg is not None:
        yield node.arg
    elif isinstance(node, ast.alias):
        yield node.asname or node.name.rpartition(".")[2]


def violations(source, module_name, is_package=False):
    """Every boundary violation in ``source``, read as module ``module_name``."""
    tree = ast.parse(source)
    docstrings = _docstring_nodes(tree)
    found = []
    for node in ast.walk(tree):
        line = getattr(node, "lineno", "?")
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for name in _imported_names(node, module_name, is_package):
                if _outside_agent(name):
                    found.append("line %s imports %s" % (line, name))
                if DOMAIN_WORDS & set(name.lower().split(".")):
                    found.append("line %s imports the 3D domain: %s" % (line, name))
        for identifier in _identifiers(node):
            if DOMAIN_WORDS & set(identifier.lower().split("_")):
                found.append("line %s names the 3D domain: %s" % (line, identifier))
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ):
            if _MODULE_TEXT_RE.match(node.value) and _outside_agent(_module_of(node.value)):
                found.append("line %s names module %r" % (line, node.value))
            if _DOMAIN_TEXT_RE.search(node.value):
                found.append("line %s mentions the 3D domain: %r" % (line, node.value))
    return found


def test_the_agent_package_is_where_the_checker_looks():
    """Guards the check itself: a moved directory would otherwise make every
    test below pass over zero files."""
    modules = [name for _path, name, _pkg in _agent_modules()]
    assert "annealage_mesh.agent.product" in modules
    assert "annealage_mesh.agent.session.sdk" in modules
    assert "annealage_mesh.agent.http.routes_mcp" in modules


@pytest.mark.parametrize(
    "path,module_name,is_package",
    list(_agent_modules()),
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_agent_module_depends_on_nothing_of_mesh(path, module_name, is_package):
    assert violations(path.read_text(encoding="utf-8"), module_name, is_package) == []


@pytest.mark.parametrize(
    "source",
    [
        # The coupling this layout removed, re-added from sdk.py's new home.
        "from ...tools import registry\n",
        "from annealage_mesh.tools import registry\n",
        "from ... import paths\n",
        "import annealage_mesh.cli\n",
        "from ... import __version__\n",
        "def f():\n    from ...http.routes_viewer import VIEWER_HTML\n",
        'BRIDGE = "annealage_mesh.session.codex_mcp_stdio_bridge"\n',
        'ENTRY = "annealage_mesh.cli:main"\n',
        'importlib.import_module("annealage_mesh." + name)\n',
        "from ... import stl\n",
        "import trimesh\n",
        "def load_stl(path):\n    pass\n",
        'CONTENT_TYPES = {".stl": "model/stl"}\n',
    ],
)
def test_the_checker_catches_a_regression(source):
    assert violations(source, "annealage_mesh.agent.session.sdk")


@pytest.mark.parametrize(
    "source",
    [
        "from .. import product\n",
        "from ..tools import ToolServer\n",
        "from annealage_mesh.agent import files\n",
        "import asyncio\n",
        '"""A docstring may say STL, where the idea came from."""\n',
        "BRIDGE = __package__ + '.session.codex_mcp_stdio_bridge'\n",
    ],
)
def test_the_checker_allows_the_agent_layer_itself(source):
    assert violations(source, "annealage_mesh.agent.session.sdk") == []
