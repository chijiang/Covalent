from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Any

import yaml

from agent_framework.infra.settings import AppSettings
from agent_framework.skills.exceptions import SkillLoadError
from agent_framework.skills.introspection import (
    infer_bundle_resources,
    infer_bundle_scripts,
    infer_runtime_entry_point,
    infer_tools_from_entry_point,
)
from agent_framework.skills.spec import ManifestSkillSpec, SkillRuntime

_IGNORED_DIRS = {".git", "node_modules", ".venv", "__pycache__", ".tox", "dist", "build"}

logger = logging.getLogger(__name__)


class GitSkillSource:
    """Describes a git repository to sync skills from."""

    def __init__(
        self,
        url: str,
        ref: str | None = None,
        name: str | None = None,
        subdir: str | None = None,
        category: str = "github_synced",
    ) -> None:
        self.url = url
        self.ref = ref
        self.subdir = subdir.strip("/") if subdir else None
        self.category = category
        self.name = name or _repo_name_from_url(url)


class SkillLoader:
    """Discovers and loads skills from local directories and git repos."""

    def __init__(self, settings: AppSettings) -> None:
        self.settings = settings

    def discover_local(self) -> list[ManifestSkillSpec]:
        """Scan local skill directories (synchronous, safe to call anywhere)."""
        specs: list[ManifestSkillSpec] = []
        for directory in self._skill_directories():
            specs.extend(self._scan_directory(directory))
        return specs

    async def discover_git(self, payload: list[dict[str, Any]] | None = None) -> list[ManifestSkillSpec]:
        """Sync git skill sources and scan them (must be called from async context)."""
        specs: list[ManifestSkillSpec] = []
        for source in self._git_sources(payload or []):
            specs.extend(await self._sync_git_source(source))
        return specs

    def _skill_directories(self) -> list[Path]:
        explicit_directories = self.settings.skills_directories is not None
        paths: list[Path] = []
        for resolved in self.settings.local_skill_directories():
            if resolved.is_dir():
                paths.append(resolved)
            elif explicit_directories:
                logger.warning("Skills directory does not exist or is not a directory: %s", resolved)
        return paths

    def _git_sources(self, raw: list[dict[str, Any]]) -> list[GitSkillSource]:
        sources: list[GitSkillSource] = []
        for item in raw:
            normalized = normalize_git_source_payload(item)
            if normalized is not None:
                sources.append(
                    GitSkillSource(
                        url=normalized["url"],
                        ref=normalized.get("ref"),
                        name=normalized.get("name"),
                        subdir=normalized.get("subdir"),
                        category=normalized.get("category", "github_synced"),
                    )
                )
        return sources

    def _scan_directory(self, directory: Path) -> list[ManifestSkillSpec]:
        results: list[ManifestSkillSpec] = []
        try:
            skill_dirs = sorted(
                {
                    path.parent
                    for pattern in ("SKILL.md", "skill.yaml")
                    for path in directory.rglob(pattern)
                    if not _IGNORED_DIRS.intersection(path.parts)
                }
            )
        except OSError as exc:
            logger.warning("Failed to scan skills directory %s: %s", directory, exc)
            return results
        for child in skill_dirs:
            try:
                spec = self.load_skill_dir(child)
                results.append(spec)
            except SkillLoadError as exc:
                logger.error("Failed to load skill from %s: %s", child, exc)
        return results

    def load_skill_dir(self, source_dir: Path) -> ManifestSkillSpec:
        manifest_path = source_dir / "skill.yaml"
        skill_md_path = source_dir / "SKILL.md"
        markdown_meta, markdown_body = self._load_skill_markdown(skill_md_path) if skill_md_path.is_file() else ({}, "")
        if manifest_path.is_file():
            return self._load_manifest(manifest_path, source_dir, markdown_meta=markdown_meta, markdown_body=markdown_body)
        if skill_md_path.is_file():
            return self._build_default_manifest(source_dir, markdown_meta, markdown_body)
        raise SkillLoadError(f"No SKILL.md or skill.yaml found in {source_dir}")

    def _load_manifest(
        self,
        path: Path,
        source_dir: Path,
        markdown_meta: dict[str, Any] | None = None,
        markdown_body: str = "",
    ) -> ManifestSkillSpec:
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise SkillLoadError(f"Invalid YAML in {path}: {exc}") from exc

        if not isinstance(raw, dict):
            raise SkillLoadError(f"Manifest at {path} must be a YAML mapping")

        try:
            spec = ManifestSkillSpec.model_validate({**raw, "source_dir": str(source_dir)})
        except Exception as exc:
            raise SkillLoadError(f"Invalid manifest at {path}: {exc}") from exc
        if markdown_meta:
            spec = self._merge_skill_markdown(spec, markdown_meta, markdown_body)
        self._validate_manifest(spec, source_dir)
        return spec

    def _validate_manifest(self, spec: ManifestSkillSpec, source_dir: Path) -> None:
        if spec.runtime is not None:
            entry = source_dir / spec.runtime.entry_point
            if not entry.exists():
                raise SkillLoadError(
                    f"Skill '{spec.name}': entry point '{spec.runtime.entry_point}' not found in {source_dir}"
                )
        tool_names = [t.name for t in spec.tools]
        if len(tool_names) != len(set(tool_names)):
            raise SkillLoadError(f"Skill '{spec.name}': duplicate tool names in manifest")
        script_names = [script.name for script in spec.scripts]
        if len(script_names) != len(set(script_names)):
            raise SkillLoadError(f"Skill '{spec.name}': duplicate script names in manifest")
        if spec.runtime is None and spec.tools:
            raise SkillLoadError(f"Skill '{spec.name}': tools require a runtime entry point")
        for script in spec.scripts:
            path = source_dir / script.path
            if not path.exists():
                raise SkillLoadError(
                    f"Skill '{spec.name}': script '{script.path}' not found in {source_dir}"
                )
        for relative_path in set(spec.resource_files) | set(spec.eager_resource_files):
            path = source_dir / relative_path
            if not path.exists():
                raise SkillLoadError(
                    f"Skill '{spec.name}': resource '{relative_path}' not found in {source_dir}"
                )

    async def _sync_git_source(self, source: GitSkillSource) -> list[ManifestSkillSpec]:
        target_dir = self.settings.managed_skill_directory(source.category) / source.name
        if target_dir.exists():
            await self._git_pull(target_dir, source.ref)
        else:
            await self._git_clone(source.url, source.ref, target_dir)
        scan_root = target_dir / source.subdir if source.subdir else target_dir
        if not scan_root.exists():
            logger.warning("Git skill source subdir does not exist: %s", scan_root)
            return []
        specs = self._scan_directory(scan_root)
        return [
            spec.model_copy(update={"source_type": "git", "git_url": source.url, "git_ref": source.ref})
            for spec in specs
        ]

    async def _git_clone(self, url: str, ref: str | None, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        cmd = ["git", "clone", "--depth", "1"]
        if ref:
            cmd.extend(["--branch", ref])
        cmd.extend([url, str(target)])
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        await proc.wait()
        if proc.returncode != 0:
            logger.warning("git clone failed for %s", url)

    async def _git_pull(self, repo_dir: Path, ref: str | None) -> None:
        cmd = ["git", "-C", str(repo_dir), "pull", "--ff-only"]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        await proc.wait()
        if proc.returncode != 0:
            logger.warning("git pull failed for %s", repo_dir)
        elif ref:
            await self._git_checkout(repo_dir, ref)

    async def _git_checkout(self, repo_dir: Path, ref: str) -> None:
        if not re.match(r'^[\w./@-]+$', ref):
            logger.warning("Refusing git checkout with suspicious ref: %s", ref)
            return
        proc = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(repo_dir),
            "checkout",
            "--",
            ref,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.wait()
        if proc.returncode != 0:
            logger.warning("git checkout failed for %s at ref %s", repo_dir, ref)

    def _build_default_manifest(
        self,
        source_dir: Path,
        markdown_meta: dict[str, Any],
        markdown_body: str,
    ) -> ManifestSkillSpec:
        runtime = None
        tools = []
        inferred = infer_runtime_entry_point(source_dir)
        if inferred:
            runtime_type, entry_point = inferred
            runtime = SkillRuntime(type=runtime_type, protocol="callable", entry_point=entry_point)
            try:
                tools = infer_tools_from_entry_point(runtime_type, source_dir / entry_point)
            except Exception as exc:
                raise SkillLoadError(f"Failed to introspect entry point '{entry_point}' in {source_dir}: {exc}") from exc
        name = str(markdown_meta.get("name") or source_dir.name)
        description = str(markdown_meta.get("description") or _description_from_markdown(markdown_body, source_dir.name))
        references = _coerce_str_list(markdown_meta.get("references"))
        resource_files, eager_resource_files = infer_bundle_resources(source_dir)
        scripts = infer_bundle_scripts(source_dir)
        eager_override = _coerce_str_list(markdown_meta.get("eager_resources"))
        return ManifestSkillSpec.model_validate(
            {
                "name": name,
                "version": str(markdown_meta.get("version") or "0.1.0"),
                "description": description,
                "instructions": markdown_body.strip(),
                "references": references,
                "resource_files": resource_files,
                "eager_resource_files": eager_override or eager_resource_files,
                "scripts": [script.model_dump() for script in scripts],
                "tools": [tool.model_dump() for tool in tools],
                "runtime": runtime.model_dump() if runtime else None,
                "source_dir": str(source_dir),
            }
        )

    def _merge_skill_markdown(
        self,
        spec: ManifestSkillSpec,
        markdown_meta: dict[str, Any],
        markdown_body: str,
    ) -> ManifestSkillSpec:
        payload = spec.model_dump()
        if markdown_body.strip():
            payload["instructions"] = markdown_body.strip()
        if markdown_meta.get("description"):
            payload["description"] = str(markdown_meta["description"])
        elif not payload.get("description"):
            payload["description"] = _description_from_markdown(markdown_body, spec.name)
        payload["name"] = str(markdown_meta.get("name") or payload["name"])
        if markdown_meta.get("references") is not None:
            payload["references"] = _coerce_str_list(markdown_meta.get("references"))
        if markdown_meta.get("eager_resources") is not None:
            payload["eager_resource_files"] = _coerce_str_list(markdown_meta.get("eager_resources"))
        if payload.get("runtime") and not payload.get("tools"):
            runtime = payload["runtime"]
            try:
                tools = infer_tools_from_entry_point(runtime["type"], Path(spec.source_dir or ".") / runtime["entry_point"])
            except Exception as exc:
                raise SkillLoadError(f"Failed to introspect entry point for skill '{spec.name}': {exc}") from exc
            payload["tools"] = [tool.model_dump() for tool in tools]
            for tool in payload["tools"]:
                tool.setdefault("handler", tool["name"])
        if not payload.get("scripts"):
            payload["scripts"] = [script.model_dump() for script in infer_bundle_scripts(Path(spec.source_dir or "."))]
        if not payload.get("resource_files"):
            resource_files, eager_resource_files = infer_bundle_resources(Path(spec.source_dir or "."))
            payload["resource_files"] = resource_files
            if not payload.get("eager_resource_files"):
                payload["eager_resource_files"] = eager_resource_files
        return ManifestSkillSpec.model_validate(payload)

    def _load_skill_markdown(self, path: Path) -> tuple[dict[str, Any], str]:
        text = path.read_text(encoding="utf-8")
        if text.startswith("---\n"):
            _, rest = text.split("---\n", 1)
            if "\n---\n" in rest:
                frontmatter, body = rest.split("\n---\n", 1)
                raw = yaml.safe_load(frontmatter) or {}
                if not isinstance(raw, dict):
                    raise SkillLoadError(f"Frontmatter in {path} must be a YAML mapping")
                return raw, body
            else:
                logger.warning("SKILL.md at %s starts with '---' but has no closing frontmatter delimiter", path)
        return {}, text


def _repo_name_from_url(url: str) -> str:
    return url.rstrip("/").split("/")[-1].removesuffix(".git")


def _normalize_git_source(
    url: str,
    ref: str | None,
    subdir: str | None,
) -> tuple[str, str | None, str | None]:
    match = re.match(
        r"^https?://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/tree/(?P<tree_ref>[^/]+)/(?P<tree_subdir>.+)$",
        url,
    )
    if not match:
        return url, ref, subdir
    normalized_url = f"https://github.com/{match.group('owner')}/{match.group('repo')}.git"
    normalized_ref = ref or match.group("tree_ref")
    normalized_subdir = subdir or match.group("tree_subdir")
    return normalized_url, normalized_ref, normalized_subdir


def normalize_git_source_payload(item: dict[str, Any]) -> dict[str, Any] | None:
    if item.get("source_type", item.get("type", "git")) != "git" or not item.get("url"):
        return None
    url, ref, subdir = _normalize_git_source(
        str(item["url"]),
        item.get("ref"),
        item.get("subdir"),
    )
    return {
        "source_type": "git",
        "category": str(item.get("category") or "github_synced"),
        "name": item.get("name"),
        "url": url,
        "ref": ref,
        "subdir": subdir.strip("/") if isinstance(subdir, str) and subdir.strip() else None,
    }


def _description_from_markdown(body: str, fallback: str) -> str:
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            stripped = stripped.lstrip("#").strip()
        if stripped:
            return stripped
    return fallback


def _coerce_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    return [str(value)]
