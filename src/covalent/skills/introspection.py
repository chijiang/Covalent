from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

from agent_framework.skills.spec import ScriptDeclaration, ToolDeclaration


def infer_runtime_entry_point(skill_dir: Path) -> tuple[str, str] | None:
    candidates = [
        ("python", "src/main.py"),
        ("python", "main.py"),
        ("nodejs", "src/main.js"),
        ("nodejs", "main.js"),
        ("nodejs", "index.js"),
    ]
    for runtime_type, relative_path in candidates:
        if (skill_dir / relative_path).is_file():
            return runtime_type, relative_path
    return None


def infer_bundle_scripts(skill_dir: Path) -> list[ScriptDeclaration]:
    scripts_dir = skill_dir / "scripts"
    if not scripts_dir.is_dir():
        return []
    declarations: list[ScriptDeclaration] = []
    seen_names: set[str] = set()
    for path in sorted(scripts_dir.rglob("*")):
        if not path.is_file():
            continue
        runtime = _runtime_for_script(path)
        if runtime is None:
            continue
        relative_path = str(path.relative_to(skill_dir)).replace("\\", "/")
        name = _script_name(relative_path, path.stem, seen_names)
        seen_names.add(name)
        declarations.append(
            ScriptDeclaration(
                name=name,
                path=relative_path,
                description=f"Run bundled script '{relative_path}'",
                runtime=runtime,
            )
        )
    return declarations


def infer_bundle_resources(skill_dir: Path) -> tuple[list[str], list[str]]:
    resource_files: list[str] = []
    eager_files: list[str] = []
    for directory_name in ("references", "resources", "assets"):
        root = skill_dir / directory_name
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if path.is_file():
                resource_files.append(str(path.relative_to(skill_dir)).replace("\\", "/"))
    for path in sorted(skill_dir.glob("*.md")):
        if path.name == "SKILL.md":
            continue
        resource_files.append(path.name)
    quickstart = skill_dir / "references" / "quickstart.md"
    if quickstart.is_file():
        eager_files.append(str(quickstart.relative_to(skill_dir)).replace("\\", "/"))
    return sorted(dict.fromkeys(resource_files)), sorted(dict.fromkeys(eager_files))


def infer_tools_from_entry_point(runtime_type: str, entry_point: Path) -> list[ToolDeclaration]:
    if runtime_type == "python":
        return _infer_python_tools(entry_point)
    if runtime_type == "nodejs":
        return _infer_node_tools(entry_point)
    return []


def _infer_python_tools(entry_point: Path) -> list[ToolDeclaration]:
    tree = ast.parse(entry_point.read_text(encoding="utf-8"))
    tools: list[ToolDeclaration] = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name.startswith("_"):
            continue
        parameters = _python_parameters_schema(node)
        tools.append(
            ToolDeclaration(
                name=node.name,
                handler=node.name,
                description=ast.get_docstring(node) or f"Tool '{node.name}'",
                parameters=parameters,
            )
        )
    return tools


def _python_parameters_schema(node: ast.FunctionDef | ast.AsyncFunctionDef) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    required: list[str] = []
    positional_args = list(node.args.args)
    defaults = list(node.args.defaults)
    default_offset = len(positional_args) - len(defaults)
    default_map = {arg.arg: default for arg, default in zip(positional_args[default_offset:], defaults)}

    for arg in positional_args:
        if arg.arg in {"self", "cls"}:
            continue
        schema: dict[str, Any] = {"type": _python_annotation_to_json_type(arg.annotation)}
        if arg.arg not in default_map:
            required.append(arg.arg)
        else:
            default_value = _literal_or_none(default_map[arg.arg])
            if default_value is not None:
                schema["default"] = default_value
        properties[arg.arg] = schema

    result: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        result["required"] = required
    return result


def _python_annotation_to_json_type(annotation: ast.expr | None) -> str:
    if annotation is None:
        return "string"
    if isinstance(annotation, ast.Name):
        return {
            "str": "string",
            "int": "integer",
            "float": "number",
            "bool": "boolean",
            "dict": "object",
            "list": "array",
        }.get(annotation.id, "string")
    return "string"


def _literal_or_none(node: ast.expr) -> Any:
    try:
        return ast.literal_eval(node)
    except Exception:
        return None


def _infer_node_tools(entry_point: Path) -> list[ToolDeclaration]:
    source = entry_point.read_text(encoding="utf-8")
    exported = _collect_node_exported_names(source)
    function_docs = _collect_node_function_docs(source)
    tools: list[ToolDeclaration] = []
    for name in sorted(exported):
        tools.append(
            ToolDeclaration(
                name=name,
                handler=name,
                description=function_docs.get(name) or f"Tool '{name}'",
                parameters={"type": "object", "properties": {}},
            )
        )
    return tools


def _collect_node_exported_names(source: str) -> set[str]:
    patterns = [
        r"module\.exports\s*=\s*\{([^}]*)\}",
        r"export\s*\{([^}]*)\}",
    ]
    names: set[str] = set()
    for pattern in patterns:
        for match in re.finditer(pattern, source, re.MULTILINE | re.DOTALL):
            for part in match.group(1).split(","):
                raw = part.strip()
                if not raw:
                    continue
                names.add(raw.split(":")[0].strip())
    for pattern in [
        r"exports\.([A-Za-z_$][A-Za-z0-9_$]*)\s*=",
        r"module\.exports\.([A-Za-z_$][A-Za-z0-9_$]*)\s*=",
        r"export\s+(?:async\s+)?function\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*\(",
    ]:
        for match in re.finditer(pattern, source):
            names.add(match.group(1))
    return {name for name in names if not name.startswith("_")}


def _collect_node_function_docs(source: str) -> dict[str, str]:
    docs: dict[str, str] = {}
    pattern = re.compile(
        r"/\*\*(?P<doc>.*?)\*/\s*(?:export\s+)?(?:async\s+)?function\s+(?P<name>[A-Za-z_$][A-Za-z0-9_$]*)\s*\(",
        re.DOTALL,
    )
    for match in pattern.finditer(source):
        doc = " ".join(
            line.strip().lstrip("*").strip()
            for line in match.group("doc").splitlines()
            if line.strip().lstrip("*").strip()
        )
        docs[match.group("name")] = doc
    return docs


def _runtime_for_script(path: Path) -> str | None:
    return {
        ".py": "python",
        ".js": "nodejs",
        ".sh": "bash",
    }.get(path.suffix)


def _script_name(relative_path: str, stem: str, seen_names: set[str]) -> str:
    if stem not in seen_names:
        return stem
    sanitized = relative_path.replace("/", "__").replace(".", "_")
    if sanitized not in seen_names:
        return sanitized
    counter = 2
    while f"{sanitized}_{counter}" in seen_names:
        counter += 1
    return f"{sanitized}_{counter}"
