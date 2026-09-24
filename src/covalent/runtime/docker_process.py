"""Compatibility import; implementation lives in covalent_execution_docker.process."""
import importlib as _importlib
import sys as _sys

_sys.modules[__name__] = _importlib.import_module("covalent_execution_docker.process")
