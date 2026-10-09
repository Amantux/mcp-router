"""MCP Intelligent Routing Platform."""

from importlib.metadata import PackageNotFoundError, version

# Single source of truth is pyproject.toml (D6); read from the installed
# distribution so no literal can drift. Running from a bare source tree that
# was never installed reports "0+unknown" rather than crashing on import.
try:
    __version__: str = version("mcprouter")
except PackageNotFoundError:  # pragma: no cover - only without `pip install -e .`
    __version__ = "0+unknown"
