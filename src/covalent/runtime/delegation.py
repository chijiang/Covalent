"""Compatibility import; implementation lives in covalent_runtime.ports.delegation."""
import importlib as _importlib
import sys as _sys

_sys.modules[__name__] = _importlib.import_module("covalent_runtime.ports.delegation")
