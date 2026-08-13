"""Annealage Mesh: build 3D-printable parts with an agent, by pointing at them."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("annealage-mesh")
except PackageNotFoundError:  # pragma: no cover - a source tree never installed
    # Running straight from a checkout with nothing installed. Nothing in this
    # package needs a real version to work; what reads it is `--version`, the
    # `Server` header, the diagnostics block and the MCP server's registration,
    # all of which are better off saying something honest than failing.
    __version__ = "0.0.0+unknown"
