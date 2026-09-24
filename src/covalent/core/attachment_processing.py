"""Compatibility import; implementation lives in covalent_agent_kit.tools.attachment_processing."""
import importlib as _importlib
import sys as _sys

_sys.modules[__name__] = _importlib.import_module("covalent_agent_kit.tools.attachment_processing")
