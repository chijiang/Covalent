"""Compatibility import; implementation lives in covalent_enterprise.cli.commands.mcp."""
import importlib as _importlib
import sys as _sys

_sys.modules[__name__] = _importlib.import_module("covalent_enterprise.cli.commands.mcp")
