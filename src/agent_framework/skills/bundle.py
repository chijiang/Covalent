from __future__ import annotations

import base64
from pathlib import Path

from agent_framework.skills.spec import ManifestSkillSpec, ScriptDeclaration

_TEXT_READ_LIMIT = 24_000


class SkillBundleError(ValueError):
    pass


class SkillBundle:
    def __init__(self, spec: ManifestSkillSpec) -> None:
        if not spec.source_dir:
            raise SkillBundleError(f"Skill '{spec.name}' does not have a source directory")
        self.spec = spec
        self.root = Path(spec.source_dir).resolve()

    def list_files(self, kind: str = "all") -> dict[str, list[str]]:
        if kind not in {"all", "resources", "scripts"}:
            raise SkillBundleError(f"Unsupported bundle listing kind: {kind}")
        payload = {
            "resources": sorted(self.spec.resource_files),
            "scripts": [script.path for script in self.spec.scripts],
        }
        if kind == "all":
            return payload
        return {kind: payload[kind]}

    def script_for_name(self, name: str) -> ScriptDeclaration:
        for script in self.spec.scripts:
            if script.name == name:
                return script
        raise SkillBundleError(f"Skill '{self.spec.name}' does not declare a script named '{name}'")

    def resolve_resource(self, relative_path: str) -> Path:
        if relative_path not in self.spec.resource_files and relative_path not in self.spec.eager_resource_files:
            raise SkillBundleError(
                f"Skill '{self.spec.name}' does not expose resource '{relative_path}'"
            )
        return self.resolve_path(relative_path)

    def resolve_path(self, relative_path: str) -> Path:
        candidate = (self.root / relative_path).resolve()
        if self.root != candidate and self.root not in candidate.parents:
            raise SkillBundleError(f"Path escapes skill directory: {relative_path}")
        return candidate

    def read_resource(self, relative_path: str, max_bytes: int = _TEXT_READ_LIMIT) -> dict[str, object]:
        path = self.resolve_resource(relative_path)
        data = path.read_bytes()
        truncated = len(data) > max_bytes
        if truncated:
            data = data[:max_bytes]
        try:
            content = data.decode("utf-8")
            encoding = "utf-8"
        except UnicodeDecodeError:
            content = base64.b64encode(data).decode("ascii")
            encoding = "base64"
        return {
            "path": relative_path,
            "encoding": encoding,
            "content": content,
            "truncated": truncated,
        }

    def render_prompt_index(self) -> str:
        sections: list[str] = []
        if self.spec.scripts:
            lines = [
                f"- {script.name}: {script.description or script.path}"
                for script in self.spec.scripts
            ]
            sections.append(
                "This skill exposes bundled scripts via run_skill_script:\n" + "\n".join(lines)
            )
        if self.spec.resource_files:
            lines = [f"- {path}" for path in self.spec.resource_files]
            sections.append(
                "This skill exposes bundled resources via read_skill_resource:\n" + "\n".join(lines)
            )
        if not sections:
            return ""
        return "\n\n".join(sections)

    def render_eager_resources(self, max_chars_per_file: int = 4_000) -> str:
        if not self.spec.eager_resource_files:
            return ""
        blocks: list[str] = []
        for relative_path in self.spec.eager_resource_files:
            payload = self.read_resource(relative_path, max_bytes=max_chars_per_file)
            suffix = "\n[truncated]" if payload["truncated"] else ""
            blocks.append(f"## Resource: {relative_path}\n{payload['content']}{suffix}")
        return "\n\n".join(blocks)