from __future__ import annotations

import base64
from pathlib import Path

from covalent.skills.spec import ManifestSkillSpec, ScriptDeclaration

_TEXT_READ_LIMIT = 24_000


class SkillBundleError(ValueError):
    pass


def slice_text_lines(
    text: str,
    *,
    start_line: int | None,
    end_line: int | None,
    max_bytes: int | None = None,
    max_chars: int | None = None,
) -> dict[str, object]:
    """按 1-indexed 行窗口切片，返回 content 与分页元数据。

    无行参数时窗口为整篇（1..total_lines）。max_bytes 按 utf-8 字节截断
    窗口、max_chars 按字符截断；截断后 end_line 只数完整行，next_offset
    指向未读完的下一行，模型按它续读即可不丢不重。
    """
    lines = text.splitlines()
    total_lines = len(lines)
    start = start_line if start_line is not None else 1
    end = end_line if end_line is not None else total_lines
    if start < 1 or end < start:
        raise SkillBundleError(f"invalid line range: start_line={start}, end_line={end}")
    if start > total_lines:
        raise SkillBundleError(f"start_line {start} exceeds total line count {total_lines}")
    content = "\n".join(lines[start - 1 : end])
    truncated = False
    if max_bytes is not None:
        encoded = content.encode("utf-8")
        if len(encoded) > max_bytes:
            content = encoded[:max_bytes].decode("utf-8", errors="ignore")
            truncated = True
    if max_chars is not None and len(content) > max_chars:
        content = content[:max_chars]
        truncated = True
    if truncated:
        end_line_effective = start - 1 + content.count("\n")
    else:
        end_line_effective = start - 1 + len(lines[start - 1 : end])
    next_offset = end_line_effective + 1 if end_line_effective < total_lines else None
    return {
        "content": content,
        "truncated": truncated,
        "total_lines": total_lines,
        "start_line": start,
        "end_line": end_line_effective,
        "next_offset": next_offset,
    }


class SkillBundle:
    def __init__(self, spec: ManifestSkillSpec) -> None:
        if not spec.source_dir:
            raise SkillBundleError(f"Skill '{spec.name}' does not have a source directory")
        self.spec = spec
        self.root = Path(spec.source_dir).resolve()

    def list_files(self, kind: str = "all") -> dict[str, list[str]]:
        if kind not in {"all", "resources", "scripts"}:
            raise SkillBundleError(f"Unsupported bundle listing kind: {kind}")
        resource_paths = sorted(set(self.spec.resource_files) | set(self.spec.eager_resource_files))
        payload = {
            "resources": resource_paths,
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

    def read_resource(
        self,
        relative_path: str,
        max_bytes: int = _TEXT_READ_LIMIT,
        start_line: int | None = None,
        end_line: int | None = None,
    ) -> dict[str, object]:
        path = self.resolve_resource(relative_path)
        data = path.read_bytes()
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            if start_line is not None or end_line is not None:
                raise SkillBundleError(
                    f"Resource '{relative_path}' is binary; line ranges require UTF-8 text"
                )
            truncated = len(data) > max_bytes
            if truncated:
                data = data[:max_bytes]
            return {
                "path": relative_path,
                "encoding": "base64",
                "content": base64.b64encode(data).decode("ascii"),
                "truncated": truncated,
            }
        return {
            "path": relative_path,
            "encoding": "utf-8",
            **slice_text_lines(text, start_line=start_line, end_line=end_line, max_bytes=max_bytes),
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
        resource_paths = sorted(set(self.spec.resource_files) | set(self.spec.eager_resource_files))
        if resource_paths:
            lines = [f"- {path}" for path in resource_paths]
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
