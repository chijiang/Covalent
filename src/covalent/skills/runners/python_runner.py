"""Compatibility import; implementation lives in covalent_execution_native.runners.python_runner."""
import importlib as _importlib
import sys as _sys

if __name__ == "__main__":
    import runpy
    runpy.run_module("covalent_execution_native.runners.python_runner", run_name="__main__")
else:
    _sys.modules[__name__] = _importlib.import_module("covalent_execution_native.runners.python_runner")
