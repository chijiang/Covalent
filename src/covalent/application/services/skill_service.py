"""Skill management / preview / access helpers.

Extracted from ``app.py``. Module-level imports stay within ``_shared`` and the
core/infra layers. Cross-group deps are resolved lazily inside the few functions
that need them (see ``_import_skill_management_payload`` and
``_extract_skill_management_payload``) to avoid import cycles with
``_config_helpers`` / ``_runtime_apply``.
"""

from __future__ import annotations

import logging
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException

from agent_framework.api._shared import ConsolePrincipalContext, RESOURCE_METADATA_FIELDS
from agent_framework.api.schemas import (
    ManagementImportResponse,
    SkillManagementItemResponse,
    SkillManagementSourceResponse,
    SkillPreviewFileResponse,
    SkillSummaryResponse,
)
from agent_framework.infra.config_store import ConfigStore, PersistedSkillSourceConfig
from agent_framework.infra.settings import AppSettings
from agent_framework.registry.registry import FrameworkRegistry
from agent_framework.runtime.backend import ExecutionBackend
from agent_framework.skills.loader import SkillLoader
from agent_framework.skills.process import SkillProcessManager
from agent_framework.skills.spec import ManifestSkillSpec

logger = logging.getLogger(__name__)

_SKILL_PREVIEW_IGNORED_DIRS = {".git", ".next", ".venv", "__pycache__", "node_modules", "venv"}
_SKILL_PREVIEW_MAX_BYTES = 128 * 1024

def _skill_category(spec: ManifestSkillSpec, settings: AppSettings) -> str:
    if spec.source_type == "git":
        return "github_synced"
    if not spec.source_dir:
        return "unknown"
    source_dir = Path(spec.source_dir).resolve()
    for category in ("built_in", "uploaded", "authored"):
        managed_dir = settings.managed_skill_directory(category).resolve()
        if source_dir == managed_dir or source_dir.is_relative_to(managed_dir):
            return category
    return "unknown"

def _matches_skill_source(spec: ManifestSkillSpec, payload: dict[str, object]) -> bool:
    return spec.git_url == payload.get("url") and spec.git_ref == payload.get("ref") and bool(
        not payload.get("subdir") or str(payload.get("subdir")) in (spec.source_dir or "")
    )

def _visible_skill_source_payload_for_spec(
    spec: ManifestSkillSpec,
    visible_sources: list[dict[str, object]],
) -> dict[str, object] | None:
    if spec.source_type != "git":
        return None
    for source in visible_sources:
        payload = PersistedSkillSourceConfig.model_validate(source).model_dump(mode="json")
        if _matches_skill_source(spec, payload):
            return payload
    return None

def _can_access_manifest_skill(
    spec: ManifestSkillSpec,
    settings: AppSettings,
    source_payload: dict[str, object] | None,
) -> bool:
    if spec.source_type == "git":
        return source_payload is not None
    return _skill_category(spec, settings) in {"built_in", "uploaded", "authored", "unknown"}

def _skill_publication_metadata(source_payload: dict[str, object] | None) -> dict[str, object]:
    if source_payload is None:
        return {}
    return {
        "publication_resource_name": source_payload.get("name"),
        **{field: source_payload.get(field) for field in RESOURCE_METADATA_FIELDS if field in source_payload},
    }

def _manifest_skill_summary_response(
    registry: FrameworkRegistry,
    settings: AppSettings,
    spec: ManifestSkillSpec,
    source_payload: dict[str, object] | None = None,
) -> SkillSummaryResponse:
    return SkillSummaryResponse(
        name=spec.name,
        version=spec.version,
        description=spec.description,
        source_type=spec.source_type,
        category=_skill_category(spec, settings),
        source_dir=spec.source_dir,
        runtime_type=spec.runtime.type if spec.runtime else None,
        tools=[tool.name for tool in spec.tools],
        references=spec.references,
        enabled=registry.is_skill_enabled(spec.name),
        **_skill_publication_metadata(source_payload),
    )

def _inline_skill_summary_response(
    registry: FrameworkRegistry,
    spec: Any,
) -> SkillSummaryResponse:
    return SkillSummaryResponse(
        name=spec.name,
        version=spec.metadata.get("version", "0.0.0"),
        description=spec.description,
        source_type="local",
        runtime_type="python",
        tools=spec.tools,
        references=[],
        enabled=registry.is_skill_enabled(spec.name),
    )

async def _ensure_skill_access(
    app: FastAPI,
    skill_name: str,
    principal: ConsolePrincipalContext,
) -> None:
    registry: FrameworkRegistry = app.state.registry
    manifest = registry.manifest_skills.get(skill_name)
    if manifest is None:
        if skill_name in registry.skills:
            return
        raise HTTPException(status_code=404, detail=f"Unknown skill: {skill_name}")

    source_payload = _visible_skill_source_payload_for_spec(
        manifest,
        await app.state.config_store.get_document("skill_sources", principal.config),
    )
    if not _can_access_manifest_skill(manifest, app.state.settings, source_payload):
        raise HTTPException(status_code=404, detail=f"Unknown skill: {skill_name}")

async def _ensure_skill_state_mutation_allowed(
    app: FastAPI,
    skill_name: str,
    principal: ConsolePrincipalContext,
) -> None:
    if principal.is_admin:
        return

    registry: FrameworkRegistry = app.state.registry
    settings: AppSettings = app.state.settings
    manifest = registry.manifest_skills.get(skill_name)
    if manifest is None:
        raise HTTPException(status_code=403, detail="Only admins can change global skill state")
    if _skill_category(manifest, settings) != "github_synced":
        raise HTTPException(status_code=403, detail="Only admins can change global skill state")

    source_payload = _visible_skill_source_payload_for_spec(
        manifest,
        await app.state.config_store.get_document("skill_sources", principal.config),
    )
    if not source_payload or source_payload.get("owner_user_id") != principal.user_id:
        raise HTTPException(status_code=403, detail="Only the skill owner can change this skill state")

async def _set_skill_enabled(app: FastAPI, skill_name: str, enabled: bool) -> None:
    registry: FrameworkRegistry = app.state.registry
    config_store: ConfigStore = app.state.config_store

    if skill_name not in registry.skills:
        raise HTTPException(status_code=404, detail=f"Unknown skill: {skill_name}")

    registry.set_skill_enabled(skill_name, enabled)
    await config_store.set_skill_enabled(skill_name, enabled)

    if not enabled and registry.skill_process_manager is not None:
        await registry.skill_process_manager.stop_skill(skill_name)

    await _reconcile_skill_process_manager(registry, app.state.execution_backend)

async def _sync_registry_skill_states(registry: FrameworkRegistry, config_store: ConfigStore) -> None:
    registry.sync_skill_enabled_states(await config_store.get_skill_state_map())

async def _reconcile_skill_process_manager(registry: FrameworkRegistry, backend: ExecutionBackend) -> None:
    if registry.has_executable_skills(enabled_only=True):
        if registry.skill_process_manager is None:
            registry.skill_process_manager = SkillProcessManager(backend=backend)
            await registry.skill_process_manager.start()
        return

    if registry.skill_process_manager is not None:
        await registry.skill_process_manager.stop()
        registry.skill_process_manager = None

def _infer_skill_install_source_type(settings: AppSettings, source: str) -> str:
    resolved = settings.resolve_path(source)
    if resolved is not None and resolved.is_dir():
        return "directory"
    return "git"

def _detect_uploaded_skill_directory(root: Path) -> Path | None:
    if (root / "SKILL.md").is_file() or (root / "skill.yaml").is_file():
        return root

    candidates = sorted(
        {
            path.parent
            for pattern in ("SKILL.md", "skill.yaml")
            for path in root.rglob(pattern)
            if ".git" not in path.parts and "__MACOSX" not in path.parts
        }
    )
    if len(candidates) != 1:
        return None
    return candidates[0]

def _find_matching_git_skill(
    registry: FrameworkRegistry,
    source_payload: dict[str, object],
) -> ManifestSkillSpec | None:
    for spec in registry.manifest_skills.values():
        if spec.source_type != "git":
            continue
        if spec.git_url != source_payload.get("url"):
            continue
        if spec.git_ref != source_payload.get("ref"):
            continue
        source_dir = Path(spec.source_dir or "")
        subdir = source_payload.get("subdir")
        if subdir and subdir not in source_dir.as_posix():
            continue
        return spec
    return None

def _build_skill_export_zip(source_dir: Path, output_path: str) -> None:
    """Synchronous helper that walks a skill source dir and writes a zip to
    ``output_path``. Run via anyio.to_thread.run_sync so the rglob + zip
    compression doesn't block the event loop on large skills.
    """
    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for file_path in sorted(source_dir.rglob("*")):
            if not file_path.is_file():
                continue
            if "__pycache__" in file_path.parts or file_path.suffix == ".pyc":
                continue
            archive.write(file_path, file_path.relative_to(source_dir))

def _build_skill_management_source(
    spec: ManifestSkillSpec,
    category: str,
    skill_sources: list[dict[str, object]],
) -> SkillManagementSourceResponse:
    if spec.source_type == "git":
        matched_source = next((item for item in skill_sources if _matches_skill_source(spec, item)), None)
        return SkillManagementSourceResponse(
            type="git",
            category="github_synced",
            url=spec.git_url,
            ref=spec.git_ref,
            subdir=str(matched_source.get("subdir")) if matched_source and matched_source.get("subdir") else None,
            name=str(matched_source.get("name")) if matched_source and matched_source.get("name") else None,
        )

    if category == "built_in":
        return SkillManagementSourceResponse(type="built_in", category="built_in")
    if category in {"uploaded", "authored"}:
        return SkillManagementSourceResponse(type="managed", category=category)
    return SkillManagementSourceResponse(type="unknown", category="unknown")

async def _build_skill_management_export_payload(app: FastAPI, principal: ConsolePrincipalContext) -> dict[str, Any]:
    registry: FrameworkRegistry = app.state.registry
    settings: AppSettings = app.state.settings
    config_store: ConfigStore = app.state.config_store
    skill_sources = await config_store.get_document("skill_sources", principal.config)

    items: list[dict[str, Any]] = []
    for skill_name in sorted(registry.skills):
        manifest = registry.manifest_skills.get(skill_name)
        if manifest is None:
            inline_skill = registry.skills[skill_name]
            items.append(
                SkillManagementItemResponse(
                    name=inline_skill.name,
                    enabled=registry.is_skill_enabled(skill_name),
                    category="unknown",
                    source_type="local",
                    version=str(inline_skill.metadata.get("version", "")),
                    description=inline_skill.description,
                    source=SkillManagementSourceResponse(type="inline", category="unknown"),
                ).model_dump(mode="json")
            )
            continue

        category = _skill_category(manifest, settings)
        source_payload = _visible_skill_source_payload_for_spec(manifest, skill_sources)
        if not _can_access_manifest_skill(manifest, settings, source_payload):
            continue
        items.append(
            SkillManagementItemResponse(
                name=manifest.name,
                enabled=registry.is_skill_enabled(skill_name),
                category=category,
                source_type=manifest.source_type,
                version=manifest.version,
                description=manifest.description,
                source=_build_skill_management_source(manifest, category, skill_sources),
            ).model_dump(mode="json")
        )

    return {
        "version": 1,
        "kind": "skills",
        "exported_at": datetime.now(UTC).isoformat(),
        "notes": [
            "This export captures skill enablement plus git skill source configuration.",
            "Uploaded, authored, and other local skills are referenced by name only; their bundle files are not embedded.",
        ],
        "skill_sources": skill_sources,
        "items": items,
    }

def _validate_skill_management_items(items: list[object]) -> list[SkillManagementItemResponse]:
    validated: list[SkillManagementItemResponse] = []
    seen_names: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            raise HTTPException(status_code=400, detail="Skill entries must be objects")
        try:
            parsed = SkillManagementItemResponse.model_validate(item)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Invalid skill entry: {exc}") from exc
        if parsed.name in seen_names:
            raise HTTPException(status_code=400, detail=f"Duplicate skill entry: {parsed.name}")
        seen_names.add(parsed.name)
        validated.append(parsed)
    return validated

def _looks_like_skill_source_entry(item: object) -> bool:
    return isinstance(item, dict) and isinstance(item.get("url"), str) and (
        item.get("source_type") == "git" or item.get("category") == "github_synced"
    )

def _derive_skill_sources_from_skill_items(items: list[SkillManagementItemResponse]) -> list[dict[str, object]]:
    derived_sources: list[dict[str, object]] = []
    seen_keys: set[tuple[str, str | None, str | None]] = set()
    for item in items:
        if item.source.type != "git" or not item.source.url:
            continue
        source = PersistedSkillSourceConfig(
            source_type="git",
            category="github_synced",
            name=item.source.name,
            url=item.source.url,
            ref=item.source.ref,
            subdir=item.source.subdir,
        ).model_dump(mode="json")
        dedupe_key = (str(source["url"]), source.get("ref"), source.get("subdir"))
        if dedupe_key in seen_keys:
            continue
        seen_keys.add(dedupe_key)
        derived_sources.append(source)
    return derived_sources

def _extract_skill_management_payload(
    payload: Any,
) -> tuple[list[SkillManagementItemResponse], list[dict[str, object]]]:
    from .management_service import _validate_config_payload
    if isinstance(payload, list):
        if all(_looks_like_skill_source_entry(item) for item in payload):
            return [], _validate_config_payload("skill_sources", payload)
        imported_items = _validate_skill_management_items(payload)
        return imported_items, _derive_skill_sources_from_skill_items(imported_items)

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Imported skill configuration must be a YAML/JSON object or array")

    payload_kind = payload.get("kind")
    if isinstance(payload_kind, str) and payload_kind not in {"skills", "skill_sources"}:
        raise HTTPException(status_code=400, detail=f"Imported file is for '{payload_kind}', not 'skills'")

    if payload_kind == "skill_sources":
        raw_sources = payload.get("items")
        if raw_sources is None and isinstance(payload.get("data"), list):
            raw_sources = payload.get("data")
        if raw_sources is None:
            raw_sources = payload.get("skill_sources")
        if not isinstance(raw_sources, list):
            raise HTTPException(status_code=400, detail="Imported skill source payload must include an array of sources")
        return [], _validate_config_payload("skill_sources", raw_sources)

    items_raw = payload.get("items")
    if items_raw is None and isinstance(payload.get("data"), list):
        items_raw = payload.get("data")
    if items_raw is None:
        items_raw = []
    if not isinstance(items_raw, list):
        raise HTTPException(status_code=400, detail="Imported skill configuration 'items' field must be an array")

    imported_items = _validate_skill_management_items(items_raw)
    raw_sources = payload.get("skill_sources")
    if raw_sources is None:
        return imported_items, _derive_skill_sources_from_skill_items(imported_items)
    if not isinstance(raw_sources, list):
        raise HTTPException(status_code=400, detail="Imported skill configuration 'skill_sources' field must be an array")
    return imported_items, _validate_config_payload("skill_sources", raw_sources)

async def _import_skill_management_payload(
    app: FastAPI,
    payload: Any,
    principal: ConsolePrincipalContext,
) -> ManagementImportResponse:
    from .runtime_apply import _apply_runtime_config
    registry: FrameworkRegistry = app.state.registry
    config_store: ConfigStore = app.state.config_store

    imported_items, imported_sources = _extract_skill_management_payload(payload)
    saved_sources = await config_store.save_document("skill_sources", imported_sources, principal=principal.config)
    await _apply_runtime_config(app, "skill_sources", await config_store.get_document("skill_sources"))

    warnings: list[str] = []
    applied_items = 0
    seen_names: set[str] = set()

    for item in imported_items:
        if item.name in seen_names:
            raise HTTPException(status_code=400, detail=f"Duplicate skill entry: {item.name}")
        seen_names.add(item.name)

        if item.name not in registry.skills:
            warnings.append(
                (
                    f"Skill '{item.name}' is not installed in this workspace. "
                    "Its enabled state was not applied."
                )
            )
            continue

        await _ensure_skill_state_mutation_allowed(app, item.name, principal)
        registry.set_skill_enabled(item.name, item.enabled)
        await config_store.set_skill_enabled(item.name, item.enabled)
        if not item.enabled and registry.skill_process_manager is not None:
            await registry.skill_process_manager.stop_skill(item.name)
        applied_items += 1

    await _reconcile_skill_process_manager(registry, app.state.execution_backend)

    summary = (
        f"Imported {len(imported_items)} skill entries and synced {len(saved_sources)} git skill sources. "
        f"Applied state to {applied_items} installed skills."
    )
    return ManagementImportResponse(
        kind="skills",
        imported_items=len(imported_items),
        applied_items=applied_items,
        summary=summary,
        warnings=warnings,
    )

async def _reload_git_skills(app: FastAPI, payload: list[dict[str, object]]) -> None:
    registry: FrameworkRegistry = app.state.registry
    loader: SkillLoader = app.state.skill_loader
    config_store: ConfigStore = app.state.config_store

    existing_git_skills = [name for name, spec in registry.manifest_skills.items() if spec.source_type == "git"]
    if registry.skill_process_manager:
        for skill_name in existing_git_skills:
            await registry.skill_process_manager.stop_skill(skill_name)
    for skill_name in existing_git_skills:
        registry.unregister_skill(skill_name)

    for spec in await loader.discover_git(payload):
        registry.register_manifest_skill(spec)

    await _sync_registry_skill_states(registry, config_store)
    await _reconcile_skill_process_manager(registry, app.state.execution_backend)

def _language_from_path(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".md", ".markdown"}:
        return "markdown"
    if suffix in {".yml", ".yaml"}:
        return "yaml"
    if suffix == ".py":
        return "python"
    if suffix in {".js", ".mjs", ".cjs"}:
        return "javascript"
    if suffix == ".json":
        return "json"
    return "text"

def _resolve_skill_preview_paths(
    spec: ManifestSkillSpec,
    settings: AppSettings,
) -> tuple[Path | None, Path | None]:
    if not spec.source_dir:
        return None, None

    source_dir = Path(spec.source_dir).resolve()
    if not source_dir.is_dir():
        return None, None

    if spec.source_type == "git":
        managed_dir = settings.managed_skill_directory("github_synced").resolve()
        for candidate in [source_dir, *source_dir.parents]:
            if candidate.parent == managed_dir:
                return candidate, managed_dir

    return source_dir, source_dir

def _collect_skill_preview_files(
    source_dir: Path,
    path_base: Path | None = None,
) -> list[SkillPreviewFileResponse]:
    if not source_dir.is_dir():
        return []

    display_base = path_base or source_dir
    files: list[SkillPreviewFileResponse] = []
    for file_path in sorted(path for path in source_dir.rglob("*") if path.is_file()):
        relative_path = file_path.relative_to(source_dir)
        if any(part in _SKILL_PREVIEW_IGNORED_DIRS for part in relative_path.parts[:-1]):
            continue
        content = _read_skill_preview_text(file_path)
        if content is None:
            continue
        files.append(
            SkillPreviewFileResponse(
                path=str(file_path.relative_to(display_base)).replace("\\", "/"),
                language=_language_from_path(file_path),
                content=content,
            )
        )
    return files

def _read_skill_preview_text(file_path: Path) -> str | None:
    raw = file_path.read_bytes()
    if b"\x00" in raw:
        return None
    preview_bytes = raw[:_SKILL_PREVIEW_MAX_BYTES]
    try:
        content = preview_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if len(raw) > _SKILL_PREVIEW_MAX_BYTES:
        return f"{content}\n\n... truncated ...\n"
    return content

