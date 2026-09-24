"""Compatibility import; implementation lives in covalent_enterprise.api.routes.sandbox_profiles."""
import importlib as _importlib
import sys as _sys

_sys.modules[__name__] = _importlib.import_module("covalent_enterprise.api.routes.sandbox_profiles")
