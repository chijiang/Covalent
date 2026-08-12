"""Architecture boundary guard.

Enforces that ``covalent.application`` never imports from ``covalent.api`` or
FastAPI, and never reads ``app.state``. This keeps the application layer
framework-independent (see docs/architecture-assessment.md §3.5).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

APPLICATION_ROOT = Path(__file__).resolve().parents[1] / "src" / "covalent" / "application"


def _application_py_files() -> list[Path]:
    return [p for p in APPLICATION_ROOT.rglob("*.py") if p.suffix == ".py"]


@pytest.mark.parametrize("path", _application_py_files(), ids=lambda p: str(p.relative_to(APPLICATION_ROOT)))
def test_application_module_has_no_api_or_fastapi_dependency(path: Path) -> None:
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                module = alias.name.split(".")[0]
                assert module != "fastapi", f"{path} imports fastapi"
                assert module != "covalent", f"{path} imports covalent.{alias.name}"
        elif isinstance(node, ast.ImportFrom):
            if node.module is None:
                continue
            top = node.module.split(".")[0]
            assert top != "fastapi", f"{path} imports from fastapi"
            if node.module.startswith("covalent.api"):
                assert False, f"{path} imports from covalent.api ({node.module})"
        elif isinstance(node, ast.Attribute):
            # app.state access (request.app.state / app.state.X)
            if node.attr == "state" and isinstance(node.value, ast.Name):
                assert node.value.id != "app", f"{path} reads app.state"
