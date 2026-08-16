"""Sandbox profile application service.

Framework-independent orchestration over the sandbox repository: profile
input validation, one-default-per-scope, candidate revision semantics, the
bootstrap ``legacy_unverified`` seed, workspace visibility, and agent/profile
capability compatibility. Image validation runs through an injected port
(the Docker-backed adapter arrives with the image-contract task); without a
port, validation-dependent operations report unavailable instead of guessing.

The application layer never imports FastAPI or ``app.state`` here.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from typing import Any, Protocol

from covalent.application.errors import (
    ConflictError,
    NotFoundError,
    ServiceUnavailableError,
    UnprocessableEntityError,
)
from covalent.application.schemas import SandboxProfileCreateRequest, SandboxProfileUpdateRequest
from covalent.infra.sandbox_repository import SandboxRepository
from covalent.infra.settings import AppSettings

RUNTIME_CAPABILITIES = ("python", "nodejs", "shell")
PULL_POLICIES = ("never", "if_not_present", "always")
# Validation statuses that may back a live sandbox. ``legacy_unverified`` is
# bootstrap-only (the seeded compatibility default); new profiles must reach
# ``valid`` through explicit validation.
EXECUTABLE_STATUSES = ("valid", "legacy_unverified")

# Fields whose edit creates a new candidate revision (spec: image, command,
# runtime capabilities, contract version, or resources).
_RUNTIME_AFFECTING_FIELDS = (
    "image",
    "keepalive_command",
    "runtime_capabilities",
    "contract_version",
    "memory_limit",
    "pids_limit",
    "cpus",
    "tmpfs_size",
)

_SIZE_RE = re.compile(r"^(\d+(?:\.\d+)?)(b|k|m|g|t)?$", re.IGNORECASE)
_SIZE_UNITS = {"": 1, "b": 1, "k": 1024, "m": 1024**2, "g": 1024**3, "t": 1024**4}

DEFAULT_KEEPALIVE_COMMAND = ("tail", "-f", "/dev/null")


def skill_runtime_lookup_from_registry(registry: Any) -> Any:
    """A ``skill_name -> runtime`` lookup over a framework registry's manifest
    skills (``python``/``nodejs``), for agent/profile compatibility checks."""
    def lookup(skill_name: str) -> str | None:
        spec = getattr(registry, "manifest_skills", {}).get(skill_name)
        runtime = getattr(spec, "runtime", None)
        return getattr(runtime, "type", None) if runtime is not None else None
    return lookup


class SandboxImageValidationPort(Protocol):
    """Runtime port that probes a candidate image against the sandbox contract."""

    async def validate(self, candidate: dict[str, Any]) -> dict[str, Any]:
        """Return ``{"status": "valid"|"invalid", "image_id": ..., "digest": ...,
        "message": ...}`` for the candidate profile values."""
        ...


def _parse_size(value: str) -> int:
    match = _SIZE_RE.match(value.strip())
    if match is None:
        raise ValueError(f"invalid size {value!r}; expected forms like '512m', '1g', '128m'")
    return int(float(match.group(1)) * _SIZE_UNITS[(match.group(2) or "").lower()])


def _image_registry(image: str) -> str:
    """Registry host of an image reference, parsed — never substring-matched.

    Unqualified references (``library/python:3.12``) resolve to docker.io.
    """
    reference = image.split("@", 1)[0].strip()
    if "/" not in reference:
        return "docker.io"
    first = reference.split("/", 1)[0]
    if "." in first or ":" in first or first == "localhost":
        return first.lower()
    return "docker.io"


def _profile_id_from_name(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-") or "profile"
    return f"profile-{slug[:40]}-{uuid.uuid4().hex[:8]}"


class SandboxProfileService:
    def __init__(
        self,
        repository: SandboxRepository,
        settings: AppSettings,
        *,
        image_validator: SandboxImageValidationPort | None = None,
        on_profile_disabled: Any | None = None,
    ) -> None:
        self._repository = repository
        self._settings = settings
        self._image_validator = image_validator
        # Emergency-revocation hook: invoked with the profile id whenever a
        # profile becomes disabled; app wiring connects it to the binding
        # service so the profile's live containers stop immediately.
        self._on_profile_disabled = on_profile_disabled

    # --- seeding -----------------------------------------------------------

    async def ensure_seeded(self) -> None:
        """Seed the compatibility default profile from Docker settings when the
        table is empty. Idempotent; never overwrites existing profiles."""
        if await self._repository.list_profiles():
            return
        await self._repository.create_profile(
            {
                "id": "default",
                "name": "default",
                "description": "Compatibility sandbox profile seeded from deployment Docker settings.",
                "workspace_id": None,
                "image": self._settings.execution_backend_docker_image,
                "pull_policy": "if_not_present",
                "keepalive_command": list(DEFAULT_KEEPALIVE_COMMAND),
                "runtime_capabilities": ["python", "shell"],
                "contract_version": 1,
                "memory_limit": self._settings.execution_backend_docker_mem_limit,
                "pids_limit": self._settings.execution_backend_docker_pids_limit,
                "cpus": self._settings.execution_backend_docker_cpus,
                "tmpfs_size": self._settings.execution_backend_docker_tmpfs_size,
                "enabled": True,
                "is_default": True,
                "revision": 1,
                "validation_status": "legacy_unverified",
            }
        )

    # --- visibility ----------------------------------------------------------

    async def list_profiles(self, workspace_id: str | None = None) -> list[dict[str, Any]]:
        return [
            profile
            for profile in await self._repository.list_profiles()
            if self._is_visible(profile, workspace_id)
        ]

    async def get_profile(
        self, profile_id: str, workspace_id: str | None = None
    ) -> dict[str, Any]:
        profile = await self._repository.get_profile(profile_id)
        if profile is None or not self._is_visible(profile, workspace_id):
            raise NotFoundError(f"sandbox profile '{profile_id}' not found")
        return profile

    async def profile_response(
        self, profile_id: str, workspace_id: str | None = None
    ) -> dict[str, Any]:
        """API-facing profile detail: profile fields + reference counts."""
        profile = await self.get_profile(profile_id, workspace_id)
        profile["reference_counts"] = await self._repository.count_profile_references(profile_id)
        return profile

    async def profile_responses(
        self, workspace_id: str | None = None
    ) -> list[dict[str, Any]]:
        """API-facing profile list: profile fields + reference counts."""
        responses: list[dict[str, Any]] = []
        for profile in await self.list_profiles(workspace_id):
            profile["reference_counts"] = await self._repository.count_profile_references(profile["id"])
            responses.append(profile)
        return responses

    @staticmethod
    def _is_visible(profile: dict[str, Any], workspace_id: str | None) -> bool:
        return profile["workspace_id"] is None or profile["workspace_id"] == workspace_id

    # --- validation ----------------------------------------------------------

    def _validate_values(self, values: dict[str, Any]) -> None:
        image = str(values.get("image") or "").strip()
        if not image:
            raise UnprocessableEntityError("profile image must be a non-empty reference")
        command = values.get("keepalive_command") or []
        if not command or not all(isinstance(part, str) and part.strip() for part in command):
            raise UnprocessableEntityError("keepalive_command must be a non-empty command list")
        capabilities = set(values.get("runtime_capabilities") or [])
        unknown = capabilities.difference(RUNTIME_CAPABILITIES)
        if unknown:
            raise UnprocessableEntityError(f"unknown runtime capabilities: {sorted(unknown)}")
        if str(values.get("pull_policy") or "") not in PULL_POLICIES:
            raise UnprocessableEntityError("pull_policy must be one of never, if_not_present, always")
        try:
            memory_bytes = _parse_size(str(values.get("memory_limit") or ""))
            tmpfs_bytes = _parse_size(str(values.get("tmpfs_size") or ""))
        except ValueError as error:
            raise UnprocessableEntityError(str(error)) from error
        if memory_bytes <= 0 or tmpfs_bytes <= 0:
            raise UnprocessableEntityError("memory_limit and tmpfs_size must be positive")
        pids_limit = values.get("pids_limit")
        if not isinstance(pids_limit, int) or isinstance(pids_limit, bool) or pids_limit <= 0:
            raise UnprocessableEntityError("pids_limit must be a positive integer")
        cpus = values.get("cpus")
        if not isinstance(cpus, (int, float)) or isinstance(cpus, bool) or cpus <= 0:
            raise UnprocessableEntityError("cpus must be a positive number")

        self._check_hard_limits(memory_bytes=memory_bytes, pids_limit=pids_limit, cpus=float(cpus))
        self._check_registry_allowlist(image)

    def _check_hard_limits(self, *, memory_bytes: int, pids_limit: int, cpus: float) -> None:
        if self._settings.execution_backend_docker_max_memory:
            try:
                max_memory = _parse_size(self._settings.execution_backend_docker_max_memory)
            except ValueError as error:
                raise UnprocessableEntityError(str(error)) from error
            if memory_bytes > max_memory:
                raise UnprocessableEntityError(
                    f"memory_limit exceeds the operator maximum ({self._settings.execution_backend_docker_max_memory})"
                )
        max_cpus = self._settings.execution_backend_docker_max_cpus
        if max_cpus is not None and cpus > max_cpus:
            raise UnprocessableEntityError(f"cpus exceeds the operator maximum ({max_cpus})")
        max_pids = self._settings.execution_backend_docker_max_pids
        if max_pids is not None and pids_limit > max_pids:
            raise UnprocessableEntityError(f"pids_limit exceeds the operator maximum ({max_pids})")

    def _check_registry_allowlist(self, image: str) -> None:
        allowlist = [
            entry.strip().lower()
            for entry in self._settings.execution_backend_docker_allowed_image_registries or []
            if entry.strip()
        ]
        if allowlist and _image_registry(image) not in allowlist:
            raise UnprocessableEntityError(
                f"image registry '{_image_registry(image)}' is not in the allowed registries"
            )

    async def validate_profile(
        self, profile_id: str, workspace_id: str | None = None
    ) -> dict[str, Any]:
        profile = await self.get_profile(profile_id, workspace_id)
        result = await self._run_image_validation(profile)
        status = result.get("status")
        if status not in ("valid", "invalid"):
            raise ServiceUnavailableError(f"image validator returned an unusable status: {status!r}")
        updated = await self._repository.update_profile(
            profile_id,
            {
                "validation_status": status,
                "validated_image_id": result.get("image_id"),
                "validated_image_digest": result.get("digest"),
                "validated_at": datetime.now(UTC),
                "validation_message": result.get("message"),
            },
        )
        assert updated is not None
        return updated

    # --- CRUD -----------------------------------------------------------------

    async def create_profile(
        self,
        request: SandboxProfileCreateRequest,
        workspace_id: str | None = None,
    ) -> dict[str, Any]:
        values = request.model_dump()
        self._validate_values(values)
        name = values["name"].strip()
        if not name:
            raise UnprocessableEntityError("profile name must be non-empty")
        return await self._repository.create_profile(
            {
                **values,
                "id": _profile_id_from_name(name),
                "name": name,
                "workspace_id": workspace_id,
                "enabled": False,
                "is_default": False,
                "revision": 1,
                "validation_status": "pending",
            }
        )

    async def update_profile(
        self,
        profile_id: str,
        request: SandboxProfileUpdateRequest,
        workspace_id: str | None = None,
    ) -> dict[str, Any]:
        current = await self.get_profile(profile_id, workspace_id)
        changes = request.model_dump(exclude_unset=True)
        if not changes:
            return current

        validated_fields = _RUNTIME_AFFECTING_FIELDS + ("pull_policy",)
        candidate = {key: current[key] for key in validated_fields}
        candidate.update({key: changes[key] for key in validated_fields if key in changes})
        self._validate_values(candidate)

        runtime_changed = any(
            field in changes and changes[field] != current[field]
            for field in _RUNTIME_AFFECTING_FIELDS
        )
        updates: dict[str, Any] = {}
        disabled_via_update = False
        if runtime_changed:
            if current["enabled"]:
                result = await self._validate_candidate(candidate)
                if result["status"] != "valid":
                    raise ConflictError(
                        f"candidate revision rejected by image validation: {result.get('message') or 'invalid image'}"
                    )
                updates.update(
                    {
                        key: changes[key]
                        for key in _RUNTIME_AFFECTING_FIELDS
                        if key in changes
                    }
                )
                updates["revision"] = current["revision"] + 1
                updates["validation_status"] = "valid"
                updates["validated_image_id"] = result.get("image_id")
                updates["validated_image_digest"] = result.get("digest")
                updates["validated_at"] = datetime.now(UTC)
                updates["validation_message"] = result.get("message")
            else:
                # A disabled profile may save a pending candidate and validate
                # it explicitly later.
                updates.update(
                    {key: changes[key] for key in _RUNTIME_AFFECTING_FIELDS if key in changes}
                )
                updates["revision"] = current["revision"] + 1
                updates["validation_status"] = "pending"
                updates["validated_image_id"] = None
                updates["validated_image_digest"] = None
                updates["validated_at"] = None
                updates["validation_message"] = None

        if "name" in changes:
            name = str(changes["name"]).strip()
            if not name:
                raise UnprocessableEntityError("profile name must be non-empty")
            updates["name"] = name
        if "description" in changes:
            updates["description"] = changes["description"]
        if "pull_policy" in changes:
            updates["pull_policy"] = changes["pull_policy"]

        if changes.get("enabled") is True and not current["enabled"]:
            if updates.get("validation_status", current["validation_status"]) not in EXECUTABLE_STATUSES:
                raise ConflictError("profile cannot be enabled until image validation succeeds")
            updates["enabled"] = True
        elif changes.get("enabled") is False:
            updates["enabled"] = False
            disabled_via_update = True

        promote_to_default = False
        if changes.get("is_default"):
            if updates.get("validation_status", current["validation_status"]) not in EXECUTABLE_STATUSES:
                raise ConflictError("profile cannot be the default until image validation succeeds")
            if not updates.get("enabled", current["enabled"]):
                raise ConflictError("profile cannot be the default while disabled")
            promote_to_default = True
        elif changes.get("is_default") is False:
            updates["is_default"] = False

        if not updates and not promote_to_default:
            return current

        updated = await self._repository.update_profile(profile_id, updates)
        assert updated is not None
        if promote_to_default:
            # Demote the previous default and promote this profile in one
            # transaction — a failure or concurrent switch can never leave
            # zero or multiple defaults in the availability scope.
            promoted = await self._repository.set_default_profile(profile_id, current["workspace_id"])
            assert promoted
            updated = await self._repository.get_profile(profile_id)
            assert updated is not None
        assert updated is not None
        if disabled_via_update and current["enabled"]:
            await self._notify_profile_disabled(profile_id)
        return updated

    async def _run_image_validation(self, candidate: dict[str, Any]) -> dict[str, Any]:
        """Run the image validator with daemon-unavailable failures mapped to
        a clean, sanitized 503 — shared by the explicit validate path and the
        enabled-profile candidate-update path."""
        if self._image_validator is None:
            raise ServiceUnavailableError(
                "image validation is unavailable: no Docker validation adapter is configured"
            )
        try:
            return await self._image_validator.validate(candidate)
        except Exception as exc:
            if isinstance(exc, ServiceUnavailableError):
                raise
            from covalent.runtime.sandbox_image_validator import sanitize_error_message

            raise ServiceUnavailableError(
                f"image validation is unavailable: {sanitize_error_message(str(exc))}"
            ) from exc

    async def _validate_candidate(self, candidate: dict[str, Any]) -> dict[str, Any]:
        try:
            return await self._run_image_validation(candidate)
        except ServiceUnavailableError:
            raise ServiceUnavailableError(
                "candidate validation is unavailable (Docker daemon/registry unreachable); "
                "the active revision is unchanged"
            )

    async def enable_profile(self, profile_id: str, workspace_id: str | None = None) -> dict[str, Any]:
        return await self.update_profile(
            profile_id, SandboxProfileUpdateRequest(enabled=True), workspace_id=workspace_id
        )

    async def disable_profile(self, profile_id: str, workspace_id: str | None = None) -> dict[str, Any]:
        """Disable a profile. Disabling is emergency revocation: after the flag
        flips, the wiring's hook stops every live instance pinned to the profile
        and binding resolution blocks lazy container recreation."""
        await self.get_profile(profile_id, workspace_id)
        updated = await self._repository.update_profile(profile_id, {"enabled": False})
        assert updated is not None
        await self._notify_profile_disabled(profile_id)
        return updated

    async def _notify_profile_disabled(self, profile_id: str) -> None:
        if self._on_profile_disabled is None:
            return
        try:
            result = self._on_profile_disabled(profile_id)
            if result is not None and hasattr(result, "__await__"):
                await result
        except Exception:
            # The flag flip already committed; revocation hook failures must
            # surface without rolling the disable back.
            raise ConflictError(
                f"profile '{profile_id}' was disabled but stopping its live instances failed; "
                "stop them from the sandbox monitor before re-enabling"
            )

    async def delete_profile(self, profile_id: str, workspace_id: str | None = None) -> bool:
        await self.get_profile(profile_id, workspace_id)
        references = await self._repository.count_profile_references(profile_id)
        if references["agents"] or references["instances"]:
            raise ConflictError(
                f"profile '{profile_id}' is still referenced by "
                f"{references['agents']} agent(s) and {references['instances']} sandbox instance(s); disable it instead"
            )
        return await self._repository.delete_profile(profile_id)

    # --- resolution helpers ------------------------------------------------------

    async def resolve_default_profile(self, workspace_id: str | None = None) -> dict[str, Any]:
        """The enabled default profile for a workspace, preferring a
        workspace-owned default over the global one; without a workspace id,
        only global defaults apply. Seeding runs lazily so a fresh deployment
        resolves without manual setup."""
        await self.ensure_seeded()
        defaults = [
            profile
            for profile in await self._repository.list_profiles()
            if profile["is_default"] and profile["enabled"]
        ]
        if workspace_id:
            visible = [
                profile
                for profile in defaults
                if profile["workspace_id"] is None or profile["workspace_id"] == workspace_id
            ]
            workspace_default = next(
                (profile for profile in visible if profile["workspace_id"] == workspace_id), None
            )
            if workspace_default is not None:
                return workspace_default
            global_default = next((profile for profile in visible if profile["workspace_id"] is None), None)
            if global_default is not None:
                return global_default
        else:
            global_default = next(
                (profile for profile in defaults if profile["workspace_id"] is None), None
            )
            if global_default is not None:
                return global_default
        raise NotFoundError("no enabled default sandbox profile is configured for this workspace")

    def check_agent_compatibility(
        self,
        *,
        agent_name: str,
        profile: dict[str, Any],
        skill_runtimes: dict[str, str],
        shell_tool_enabled: bool,
    ) -> None:
        """Raise ``ConflictError`` naming agent, skill, and missing capability
        when the agent's executable surface exceeds the profile's capabilities."""
        capabilities = set(profile.get("runtime_capabilities") or [])
        runtime_requirement = {"python": "python", "nodejs": "nodejs"}
        for skill_name, runtime in skill_runtimes.items():
            required = runtime_requirement.get(runtime)
            if required and required not in capabilities:
                raise ConflictError(
                    f"agent '{agent_name}' assigns skill '{skill_name}' requiring runtime "
                    f"'{runtime}', but sandbox profile '{profile.get('id')}' does not declare "
                    f"capability '{required}'"
                )
        if shell_tool_enabled and "shell" not in capabilities:
            raise ConflictError(
                f"agent '{agent_name}' enables the sandbox shell tool, but sandbox profile "
                f"'{profile.get('id')}' does not declare capability 'shell'"
            )

    async def validate_agent_selections(
        self,
        agents: list[dict[str, Any]],
        *,
        workspace_id: str | None = None,
        skill_runtime_lookup: Any = None,
        shell_tool_enabled: bool = False,
    ) -> None:
        """Save-time validation for explicit profile selections: the profile
        must exist, be visible in the workspace, and cover the agent's skill
        runtimes. Agents without an explicit selection (default profile) are
        validated at binding time, where the effective runtime surface —
        including the shell tool — is known."""
        profiles: dict[str, dict[str, Any]] = {}
        for agent in agents:
            profile_id = agent.get("sandbox_profile_id")
            if not profile_id:
                continue
            if profile_id not in profiles:
                profiles[profile_id] = await self.get_profile(profile_id, workspace_id)
            profile = profiles[profile_id]
            if not profile["enabled"]:
                raise ConflictError(
                    f"agent '{agent.get('name')}' selects sandbox profile '{profile_id}', "
                    "which is disabled"
                )
            if skill_runtime_lookup is not None:
                skill_runtimes = {
                    skill_name: skill_runtime_lookup(skill_name)
                    for skill_name in agent.get("skills") or []
                    if skill_runtime_lookup(skill_name)
                }
                self.check_agent_compatibility(
                    agent_name=str(agent.get("name") or ""),
                    profile=profile,
                    skill_runtimes=skill_runtimes,
                    shell_tool_enabled=shell_tool_enabled,
                )
