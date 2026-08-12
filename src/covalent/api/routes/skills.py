"""skills route group."""

from __future__ import annotations

from fastapi import APIRouter

from fastapi import File
from fastapi import Form
from fastapi import HTTPException
from fastapi import Request
from fastapi import UploadFile
from fastapi.responses import FileResponse
from pathlib import Path
from starlette.background import BackgroundTask
from typing import Any
import anyio
import functools
import os
import shutil
import tempfile
import zipfile

from covalent.api._auth_helpers import _resolve_console_principal
from covalent.api._shared import _rmtree_async
from covalent.api._shared import _safe_extract_zip
from covalent.application.schemas import SkillInstallRequest
from covalent.application.schemas import SkillInstallResponse
from covalent.application.schemas import SkillPreviewFileResponse
from covalent.application.schemas import SkillPreviewResponse
from covalent.application.schemas import SkillSummaryResponse
from covalent.application.services.runtime_apply import _apply_runtime_config
from covalent.application.services.skill_service import _build_skill_export_zip
from covalent.application.services.skill_service import _can_access_manifest_skill
from covalent.application.services.skill_service import _collect_skill_preview_files
from covalent.application.services.skill_service import _detect_uploaded_skill_directory
from covalent.application.services.skill_service import _ensure_skill_access
from covalent.application.services.skill_service import _ensure_skill_state_mutation_allowed
from covalent.application.services.skill_service import _find_matching_git_skill
from covalent.application.services.skill_service import _infer_skill_install_source_type
from covalent.application.services.skill_service import _inline_skill_summary_response
from covalent.application.services.skill_service import _manifest_skill_summary_response
from covalent.application.services.skill_service import _matches_skill_source
from covalent.application.services.skill_service import _reconcile_skill_process_manager
from covalent.application.services.skill_service import _resolve_skill_preview_paths
from covalent.application.services.skill_service import _set_skill_enabled
from covalent.application.services.skill_service import _skill_category
from covalent.application.services.skill_service import _sync_registry_skill_states
from covalent.application.services.skill_service import _visible_skill_source_payload_for_spec
from covalent.infra.config_store import ConfigStore
from covalent.infra.config_store import PersistedSkillSourceConfig
from covalent.infra.db import DatabaseManager
from covalent.infra.settings import AppSettings
from covalent.registry.registry import FrameworkRegistry
from covalent.skills.loader import SkillLoader
from covalent.skills.loader import normalize_git_source_payload

router = APIRouter()


@router.get("/skills")
async def list_skills(request: Request) -> list[SkillSummaryResponse]:
    registry: FrameworkRegistry = request.app.state.registry
    settings: AppSettings = request.app.state.settings
    config_store: ConfigStore = request.app.state.config_store
    db_manager: DatabaseManager = request.app.state.db_manager
    principal = await _resolve_console_principal(request, db_manager)
    visible_sources = await config_store.get_document("skill_sources", principal.config)
    results: list[SkillSummaryResponse] = []
    for name, spec in registry.manifest_skills.items():
        source_payload = _visible_skill_source_payload_for_spec(spec, visible_sources)
        if not _can_access_manifest_skill(spec, settings, source_payload):
            continue
        results.append(_manifest_skill_summary_response(registry, settings, spec, source_payload))
    for name, spec in registry.skills.items():
        if name not in registry.manifest_skills:
            results.append(_inline_skill_summary_response(registry, spec))
    return results

@router.get("/skills/{skill_name}")
async def get_skill(request: Request, skill_name: str) -> SkillSummaryResponse:
    registry: FrameworkRegistry = request.app.state.registry
    settings: AppSettings = request.app.state.settings
    config_store: ConfigStore = request.app.state.config_store
    db_manager: DatabaseManager = request.app.state.db_manager
    principal = await _resolve_console_principal(request, db_manager)
    manifest = registry.manifest_skills.get(skill_name)
    if manifest:
        source_payload = _visible_skill_source_payload_for_spec(manifest, await config_store.get_document("skill_sources", principal.config))
        if not _can_access_manifest_skill(manifest, settings, source_payload):
            raise HTTPException(status_code=404, detail=f"Unknown skill: {skill_name}")
        return _manifest_skill_summary_response(registry, settings, manifest, source_payload)
    spec = registry.skills.get(skill_name)
    if spec:
        return _inline_skill_summary_response(registry, spec)
    raise HTTPException(status_code=404, detail=f"Unknown skill: {skill_name}")

@router.get("/skills/{skill_name}/preview")
async def preview_skill(request: Request, skill_name: str) -> SkillPreviewResponse:
    registry: FrameworkRegistry = request.app.state.registry
    settings: AppSettings = request.app.state.settings
    config_store: ConfigStore = request.app.state.config_store
    db_manager: DatabaseManager = request.app.state.db_manager
    principal = await _resolve_console_principal(request, db_manager)
    manifest = registry.manifest_skills.get(skill_name)
    if manifest:
        source_payload = _visible_skill_source_payload_for_spec(manifest, await config_store.get_document("skill_sources", principal.config))
        if not _can_access_manifest_skill(manifest, settings, source_payload):
            raise HTTPException(status_code=404, detail=f"Unknown skill: {skill_name}")
        preview_root, preview_path_base = _resolve_skill_preview_paths(manifest, settings)
        files = _collect_skill_preview_files(preview_root, preview_path_base) if preview_root is not None else []
        return SkillPreviewResponse(name=manifest.name, source_dir=manifest.source_dir, files=files)

    spec = registry.skills.get(skill_name)
    if spec:
        return SkillPreviewResponse(
            name=spec.name,
            files=[
                SkillPreviewFileResponse(
                    path="instructions.md",
                    language="markdown",
                    content=spec.instructions,
                )
            ],
        )

    raise HTTPException(status_code=404, detail=f"Unknown skill: {skill_name}")

@router.post("/skills/install")
async def install_skill(request: Request, install_request: SkillInstallRequest) -> SkillInstallResponse:
    registry: FrameworkRegistry = request.app.state.registry
    loader: SkillLoader = request.app.state.skill_loader
    settings: AppSettings = request.app.state.settings
    config_store: ConfigStore = request.app.state.config_store
    db_manager: DatabaseManager = request.app.state.db_manager
    principal = await _resolve_console_principal(request, db_manager)
    source_type = install_request.source_type or _infer_skill_install_source_type(settings, install_request.source)

    if source_type == "directory":
        source_dir = loader.settings.resolve_path(install_request.source)
        if source_dir is None or not source_dir.is_dir():
            raise HTTPException(status_code=400, detail=f"Source path does not exist or is not a directory: {install_request.source}")

        from covalent.skills.exceptions import SkillLoadError

        try:
            spec = loader.load_skill_dir(source_dir)
        except SkillLoadError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        if install_request.category == "github_synced":
            raise HTTPException(status_code=400, detail="Directory installs must use built_in, uploaded, or authored categories")

        target_dir = settings.managed_skill_directory(install_request.category) / spec.name
        if target_dir.exists():
            status = "already_exists"
        else:
            # Skill source dirs can be large; copy off the event loop.
            await anyio.to_thread.run_sync(
                functools.partial(shutil.copytree, source_dir, target_dir)
            )
            status = "installed"
        registered_spec = loader.load_skill_dir(target_dir)
        registry.register_manifest_skill(registered_spec)
        await _sync_registry_skill_states(registry, config_store)
        await _reconcile_skill_process_manager(registry, request.app.state.execution_backend)
        return SkillInstallResponse(
            name=registered_spec.name,
            version=registered_spec.version,
            description=registered_spec.description,
            status=status,
        )

    normalized_source = normalize_git_source_payload(
        {
            "source_type": "git",
            "category": "github_synced",
            "name": install_request.name,
            "url": install_request.source,
            "ref": install_request.ref,
            "subdir": install_request.subdir,
        }
    )
    if normalized_source is None:
        raise HTTPException(status_code=400, detail="Invalid git skill source")

    existing_sources = await config_store.get_document("skill_sources", principal.config)
    normalized_payload = PersistedSkillSourceConfig.model_validate(normalized_source).model_dump(mode="json")
    already_exists = any(
        PersistedSkillSourceConfig.model_validate(item).model_dump(mode="json") == normalized_payload
        for item in existing_sources
    )
    if not already_exists:
        existing_sources.append(normalized_payload)
        existing_sources = await config_store.save_document("skill_sources", existing_sources, principal=principal.config)
    global_sources = await config_store.get_document("skill_sources")
    await _apply_runtime_config(request.app.state.registry, request.app.state.config_store, request.app.state.settings, request.app.state.skill_loader, request.app.state.execution_backend, "skill_sources", global_sources)

    installed_spec = _find_matching_git_skill(registry, normalized_payload)
    if installed_spec is None:
        raise HTTPException(status_code=500, detail="Git skill source synced, but no loadable skill was discovered")
    if not normalized_payload.get("name"):
        normalized_payload["name"] = installed_spec.name
        patched_sources = [
            normalized_payload if PersistedSkillSourceConfig.model_validate(item).url == normalized_payload.get("url") else item
            for item in existing_sources
        ]
        await config_store.save_document("skill_sources", patched_sources, principal=principal.config)
        await _apply_runtime_config(request.app.state.registry, request.app.state.config_store, request.app.state.settings, request.app.state.skill_loader, request.app.state.execution_backend, "skill_sources", await config_store.get_document("skill_sources"))

    return SkillInstallResponse(
        name=installed_spec.name,
        version=installed_spec.version,
        description=installed_spec.description,
        status="already_exists" if already_exists else "installed",
    )

@router.post("/skills/upload")
async def upload_skill(
    request: Request,
    file: UploadFile = File(...),
    category: str = Form("uploaded"),
) -> SkillInstallResponse:
    settings: AppSettings = request.app.state.settings
    if category not in {"uploaded", "authored"}:
        raise HTTPException(status_code=400, detail="Uploaded skills must use the uploaded or authored category")
    if not file.filename or not file.filename.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="Only .zip skill bundles are currently supported")

    with tempfile.TemporaryDirectory() as temp_dir_name:
        temp_dir = Path(temp_dir_name)
        archive_path = temp_dir / file.filename
        raw_content = await file.read()
        if len(raw_content) > settings.max_upload_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"Uploaded file exceeds maximum size ({settings.max_upload_bytes} bytes)",
            )
        archive_path.write_bytes(raw_content)
        extracted_root = temp_dir / "extracted"
        extracted_root.mkdir(parents=True, exist_ok=True)
        try:
            with zipfile.ZipFile(archive_path) as archive:
                _safe_extract_zip(archive, extracted_root)
        except zipfile.BadZipFile as exc:
            raise HTTPException(status_code=400, detail="Uploaded file is not a valid zip archive") from exc

        source_dir = _detect_uploaded_skill_directory(extracted_root)
        if source_dir is None:
            raise HTTPException(status_code=400, detail="Uploaded archive must contain exactly one skill bundle")

        return await install_skill(
            request,
            SkillInstallRequest(
                source=str(source_dir),
                source_type="directory",
                category=category,
            )
        )

@router.delete("/skills/{skill_name}")
async def uninstall_skill(request: Request, skill_name: str) -> dict[str, str]:
    registry: FrameworkRegistry = request.app.state.registry
    settings: AppSettings = request.app.state.settings
    config_store: ConfigStore = request.app.state.config_store
    db_manager: DatabaseManager = request.app.state.db_manager
    principal = await _resolve_console_principal(request, db_manager)
    if skill_name not in registry.manifest_skills and skill_name not in registry.skills:
        raise HTTPException(status_code=404, detail=f"Unknown skill: {skill_name}")
    await _ensure_skill_access(request.app.state.registry, request.app.state.config_store, request.app.state.settings, skill_name, principal)

    if registry.skill_process_manager:
        await registry.skill_process_manager.stop_skill(skill_name)

    spec = registry.manifest_skills.get(skill_name)
    if spec and _skill_category(spec, settings) == "built_in":
        raise HTTPException(status_code=400, detail="Built-in skills cannot be uninstalled through the API")

    spec = registry.unregister_skill(skill_name)
    await config_store.delete_skill_state(skill_name)
    await _reconcile_skill_process_manager(registry, request.app.state.execution_backend)

    if spec:
        category = _skill_category(spec, settings)
        source_dir = Path(spec.source_dir) if spec.source_dir else None
        if spec.source_type == "git":
            existing_sources = await config_store.get_document("skill_sources", principal.config)
            remaining_sources = [
                item
                for item in existing_sources
                if not _matches_skill_source(spec, PersistedSkillSourceConfig.model_validate(item).model_dump(mode="json"))
            ]
            if len(remaining_sources) != len(existing_sources):
                await config_store.save_document("skill_sources", remaining_sources, principal=principal.config)
                await _apply_runtime_config(request.app.state.registry, request.app.state.config_store, request.app.state.settings, request.app.state.skill_loader, request.app.state.execution_backend, "skill_sources", await config_store.get_document("skill_sources"))
            still_referenced = any(
                _matches_skill_source(spec, PersistedSkillSourceConfig.model_validate(item).model_dump(mode="json"))
                for item in await config_store.get_document("skill_sources")
            )
            if source_dir and source_dir.exists() and not still_referenced:
                repo_root = settings.managed_skill_directory("github_synced")
                for candidate in [source_dir, *source_dir.parents]:
                    if candidate.parent == repo_root:
                        await _rmtree_async(candidate, ignore_errors=True)
                        break
        elif category in {"uploaded", "authored"} and source_dir and source_dir.exists():
            await _rmtree_async(source_dir, ignore_errors=True)

    return {"status": "uninstalled", "skill": skill_name}

@router.get("/skills/{skill_name}/export")
async def export_skill(request: Request, skill_name: str) -> FileResponse:
    registry: FrameworkRegistry = request.app.state.registry
    db_manager: DatabaseManager = request.app.state.db_manager
    principal = await _resolve_console_principal(request, db_manager)
    await _ensure_skill_access(request.app.state.registry, request.app.state.config_store, request.app.state.settings, skill_name, principal)
    manifest = registry.manifest_skills.get(skill_name)
    if not manifest:
        raise HTTPException(status_code=404, detail=f"Unknown skill: {skill_name}")
    if not manifest.source_dir:
        raise HTTPException(status_code=400, detail=f"Skill '{skill_name}' has no local source directory")

    source = Path(manifest.source_dir)
    if not source.is_dir():
        raise HTTPException(status_code=404, detail=f"Skill directory not found: {source}")

    tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False, prefix=f"{skill_name}_")
    try:
        await anyio.to_thread.run_sync(_build_skill_export_zip, source, tmp.name)
    except Exception:
        os.unlink(tmp.name)
        raise

    return FileResponse(
        tmp.name,
        media_type="application/zip",
        filename=f"{skill_name}.zip",
        # Unlink the on-disk zip after the response finishes streaming so
        # repeated exports don't accumulate temp files.
        background=BackgroundTask(os.unlink, tmp.name),
    )

@router.post("/skills/{skill_name}/enable")
async def enable_skill(request: Request, skill_name: str) -> dict[str, str]:
    principal = await _resolve_console_principal(request, request.app.state.db_manager)
    await _ensure_skill_access(request.app.state.registry, request.app.state.config_store, request.app.state.settings, skill_name, principal)
    await _ensure_skill_state_mutation_allowed(request.app.state.registry, request.app.state.config_store, request.app.state.settings, skill_name, principal)
    await _set_skill_enabled(request.app.state.registry, request.app.state.config_store, request.app.state.execution_backend, skill_name, True)
    return {"status": "enabled", "skill": skill_name}

@router.post("/skills/{skill_name}/disable")
async def disable_skill(request: Request, skill_name: str) -> dict[str, str]:
    principal = await _resolve_console_principal(request, request.app.state.db_manager)
    await _ensure_skill_access(request.app.state.registry, request.app.state.config_store, request.app.state.settings, skill_name, principal)
    await _ensure_skill_state_mutation_allowed(request.app.state.registry, request.app.state.config_store, request.app.state.settings, skill_name, principal)
    await _set_skill_enabled(request.app.state.registry, request.app.state.config_store, request.app.state.execution_backend, skill_name, False)
    return {"status": "disabled", "skill": skill_name}

@router.post("/skills/{skill_name}/start")
async def start_skill(request: Request, skill_name: str) -> dict[str, Any]:
    principal = await _resolve_console_principal(request, request.app.state.db_manager)
    await _ensure_skill_access(request.app.state.registry, request.app.state.config_store, request.app.state.settings, skill_name, principal)
    registry: FrameworkRegistry = request.app.state.registry
    manifest = registry.manifest_skills.get(skill_name)
    if not manifest:
        raise HTTPException(status_code=404, detail=f"Unknown manifest skill: {skill_name}")
    if not registry.is_skill_enabled(skill_name):
        raise HTTPException(status_code=400, detail=f"Skill '{skill_name}' is disabled")
    if not manifest.is_executable:
        return {"status": "not_executable", "skill": skill_name}
    if not registry.skill_process_manager:
        raise HTTPException(status_code=400, detail="Skill process manager is not initialized")

    handle = await registry.skill_process_manager.acquire(manifest)
    await registry.skill_process_manager.release(handle)
    return {"status": "started", "skill": skill_name}

@router.post("/skills/{skill_name}/stop")
async def stop_skill(request: Request, skill_name: str) -> dict[str, str]:
    principal = await _resolve_console_principal(request, request.app.state.db_manager)
    await _ensure_skill_access(request.app.state.registry, request.app.state.config_store, request.app.state.settings, skill_name, principal)
    registry: FrameworkRegistry = request.app.state.registry
    if not registry.skill_process_manager:
        return {"status": "stopped", "skill": skill_name}

    await registry.skill_process_manager.stop_skill(skill_name)
    return {"status": "stopped", "skill": skill_name}

@router.get("/skills/{skill_name}/health")
async def skill_health(request: Request, skill_name: str) -> dict[str, Any]:
    principal = await _resolve_console_principal(request, request.app.state.db_manager)
    await _ensure_skill_access(request.app.state.registry, request.app.state.config_store, request.app.state.settings, skill_name, principal)
    registry: FrameworkRegistry = request.app.state.registry
    manifest = registry.manifest_skills.get(skill_name)
    if manifest is None:
        raise HTTPException(status_code=404, detail=f"Unknown manifest skill: {skill_name}")
    if not registry.is_skill_enabled(skill_name):
        return {"status": "disabled", "pools": {}}
    if not manifest.is_executable:
        return {"status": "not_executable", "pools": {}}
    if not registry.skill_process_manager:
        return {"status": "not_loaded", "pools": {}}

    return {"status": "running", "pools": registry.skill_process_manager.pool_status(skill_name)}
