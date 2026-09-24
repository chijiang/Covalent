"""Compatibility import; implementation lives in covalent_enterprise.application.services.session_service."""
import importlib as _importlib
import sys as _sys

_sys.modules[__name__] = _importlib.import_module("covalent_enterprise.application.services.session_service")
