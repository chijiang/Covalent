from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any

from agent_framework.core.types import RunContext
from agent_framework.core.workspace_tools import _get_session_workspace_root
from agent_framework.skills.bundle import SkillBundle, SkillBundleError
from agent_framework.skills.permissions import PermissionChecker
from agent_framework.skills.spec import ManifestSkillSpec, ScriptDeclaration

LIST_SKILL_FILES_TOOL = "list_skill_files"
READ_SKILL_RESOURCE_TOOL = "read_skill_resource"
RUN_SKILL_SCRIPT_TOOL = "run_skill_script"


def register_skill_meta_tools(registry: Any, settings: Any = None) -> None:
    registry.register_local_tool(
        LIST_SKILL_FILES_TOOL,
        {
            "type": "function",
            "function": {
                "name": LIST_SKILL_FILES_TOOL,
                "description": "Lists bundled skill resources and scripts for a registered skill.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "skill": {"type": "string", "description": "Registered skill name."},
                        "kind": {
                            "type": "string",
                            "enum": ["all", "resources", "scripts"],
                            "default": "all",
                        },
                    },
                    "required": ["skill"],
                },
            },
        },
        handler=lambda args, _ctx: _list_skill_files(registry, args),
    )
    registry.register_local_tool(
        READ_SKILL_RESOURCE_TOOL,
        {
            "type": "function",
            "function": {
                "name": READ_SKILL_RESOURCE_TOOL,
                "description": "Reads a bundled skill resource file exposed by a registered skill.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "skill": {"type": "string", "description": "Registered skill name."},
                        "path": {"type": "string", "description": "Relative resource path."},
                        "max_bytes": {"type": "integer", "default": 24000, "minimum": 1},
                    },
                    "required": ["skill", "path"],
                },
            },
        },
        handler=lambda args, _ctx: _read_skill_resource(registry, args),
    )
    registry.register_local_tool(
        RUN_SKILL_SCRIPT_TOOL,
        {
            "type": "function",
            "function": {
                "name": RUN_SKILL_SCRIPT_TOOL,
                "description": "Runs a declared bundled skill script inside the session workspace or skill working directory.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "skill": {"type": "string", "description": "Registered skill name."},
                        "name": {"type": "string", "description": "Declared script name."},
                        "positional_args": {
                            "type": "array",
                            "items": {"type": "string"},
                            "default": [],
                        },
                        "named_args": {
                            "type": "object",
                            "default": {},
                        },
                        "timeout_seconds": {"type": "number", "minimum": 1},
                    },
                    "required": ["skill", "name"],
                },
            },
        },
        handler=lambda args, ctx: _run_skill_script(registry, args, ctx, settings),
    )


def _bundle_for_skill(registry: Any, skill_name: str) -> tuple[ManifestSkillSpec, SkillBundle]:
    is_skill_enabled = getattr(registry, "is_skill_enabled", None)
    if callable(is_skill_enabled) and not is_skill_enabled(skill_name):
        raise SkillBundleError(f"Skill '{skill_name}' is disabled")
    spec = registry.manifest_skills.get(skill_name)
    if spec is None:
        raise SkillBundleError(f"Unknown manifest skill: {skill_name}")
    return spec, SkillBundle(spec)


def _list_skill_files(registry: Any, args: dict[str, Any]) -> str:
    _spec, bundle = _bundle_for_skill(registry, str(args.get("skill", "")))
    kind = str(args.get("kind", "all"))
    return json.dumps(bundle.list_files(kind=kind), ensure_ascii=False, indent=2)


def _read_skill_resource(registry: Any, args: dict[str, Any]) -> str:
    _spec, bundle = _bundle_for_skill(registry, str(args.get("skill", "")))
    path = str(args.get("path", "")).strip()
    max_bytes = int(args.get("max_bytes", 24_000))
    return json.dumps(bundle.read_resource(path, max_bytes=max_bytes), ensure_ascii=False, indent=2)


async def _run_skill_script(
    registry: Any,
    args: dict[str, Any],
    context: RunContext | None,
    settings: Any = None,
) -> str:
    spec, bundle = _bundle_for_skill(registry, str(args.get("skill", "")))
    script = bundle.script_for_name(str(args.get("name", "")))
    positional_args = [str(value) for value in args.get("positional_args", [])]
    named_args = args.get("named_args", {})
    if not isinstance(named_args, dict):
        raise SkillBundleError("named_args must be an object")
    timeout = float(args.get("timeout_seconds", script.timeout_seconds))
    command = _build_script_command(bundle, script, positional_args, named_args)
    env = _build_script_env(spec)

    # Determine working directory: session workspace > skill bundle root
    cwd = str(bundle.root)
    if settings and context:
        try:
            session_workspace = _get_session_workspace_root(settings, context)
            # Ensure session workspace exists
            session_workspace.mkdir(parents=True, exist_ok=True)
            cwd = str(session_workspace)
        except (ValueError, AttributeError):
            # Fallback to bundle root if session workspace can't be determined
            pass

    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=cwd,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        process.kill()
        await process.wait()
        raise RuntimeError(
            f"Skill script '{script.name}' timed out after {timeout}s"
        ) from exc

    return json.dumps(
        {
            "ok": process.returncode == 0,
            "exit_code": process.returncode,
            "command": command,
            "stdout": stdout.decode("utf-8", errors="replace"),
            "stderr": stderr.decode("utf-8", errors="replace"),
        },
        ensure_ascii=False,
        indent=2,
    )


def _build_script_env(spec: ManifestSkillSpec) -> dict[str, str]:
    host_env = dict(os.environ)
    host_env["SKILL_DIR"] = spec.source_dir or ""
    host_env["SKILL_NAME"] = spec.name
    env = PermissionChecker.filter_env(spec, host_env)
    return PermissionChecker.inject_permission_env(spec, env)


def _build_script_command(
    bundle: SkillBundle,
    script: ScriptDeclaration,
    positional_args: list[str],
    named_args: dict[str, Any],
) -> list[str]:
    script_path = str(bundle.resolve_path(script.path))
    runtime = script.runtime or _runtime_from_path(script.path)
    if runtime == "python":
        command = [sys.executable, script_path]
    elif runtime == "nodejs":
        command = ["node", script_path]
    elif runtime == "bash":
        command = ["bash", script_path]
    else:
        raise SkillBundleError(f"Unsupported script runtime '{runtime}' for '{script.name}'")
    command.extend(positional_args)
    command.extend(_serialize_named_args(named_args))
    return command


def _serialize_named_args(named_args: dict[str, Any]) -> list[str]:
    cli_args: list[str] = []
    for key, value in named_args.items():
        flag = f"--{str(key).replace('_', '-')}"
        if value is None or value is False:
            continue
        if value is True:
            cli_args.append(flag)
            continue
        if isinstance(value, list):
            for item in value:
                cli_args.extend([flag, _stringify_arg(item)])
            continue
        cli_args.extend([flag, _stringify_arg(value)])
    return cli_args


def _stringify_arg(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _runtime_from_path(relative_path: str) -> str:
    if relative_path.endswith(".py"):
        return "python"
    if relative_path.endswith(".js"):
        return "nodejs"
    if relative_path.endswith(".sh"):
        return "bash"
    raise SkillBundleError(f"Unable to infer runtime for script path '{relative_path}'")