"""Compatibility import; implementation lives in covalent_agent_kit.skills.meta_tools."""
import importlib as _importlib
import sys as _sys

_sys.modules[__name__] = _importlib.import_module("covalent_agent_kit.skills.meta_tools")
