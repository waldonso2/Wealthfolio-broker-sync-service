"""Wealthfolio Companion: broker transactions → self-hosted Wealthfolio."""

from importlib.metadata import version

# The version lives in pyproject.toml only (#80); the installed package knows it.
__version__ = version("wealthfolio-companion")
