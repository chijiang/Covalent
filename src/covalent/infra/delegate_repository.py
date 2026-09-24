"""Compatibility import; implementation lives in covalent_enterprise.infra.delegate_repository."""
import importlib as _importlib
import sys as _sys

_sys.modules[__name__] = _importlib.import_module("covalent_enterprise.infra.delegate_repository")
