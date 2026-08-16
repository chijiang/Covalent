"""Sandbox image validation against the Covalent sandbox contract.

``DockerImageValidator`` probes a candidate profile image in a short-lived,
network-disabled, mount-free container with its entrypoint overridden: it
verifies ``/bin/sh``, each declared runtime binary, its runner file, the
configured keepalive command, and records the resolved image identity. It is
the runtime port backing profile validation — independent of the execution
backend, so a filesystem deployment with a reachable daemon can still validate
profiles.

Failures are sanitized (length-capped, credential patterns stripped) before
they ever reach a response or log; Docker daemon auth configuration never
leaves this module.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

import docker

from covalent.infra.settings import AppSettings
from covalent.runtime.backend import BackendUnavailable

logger = logging.getLogger(__name__)

_RUNNER_FILES = {"python": "/runners/python_runner.py", "nodejs": "/runners/node_runner.js"}
_VALIDATION_LABEL = "covalent.sandbox.validation"
_MESSAGE_MAX = 500
# Best-effort scrub of anything that looks like a credential in a docker error.
# Every pattern MUST have exactly one capture group (the label kept in place).
_SECRET_PATTERNS = (
    re.compile(r"(?i)((?:password|secret|token|authorization|api[_-]?key)\s*[=:]\s*)\S+"),
    re.compile(r"(?i)(bearer\s+)\S+"),
)


def sanitize_error_message(message: str) -> str:
    """Redact credential-shaped substrings and cap length before a docker-layer
    error reaches a response or log."""
    cleaned = message
    for pattern in _SECRET_PATTERNS:
        cleaned = pattern.sub(lambda m: f"{m.group(1)}<redacted>", cleaned)
    return cleaned[:_MESSAGE_MAX]


class DockerImageValidator:
    """Validates a candidate sandbox image against the sandbox contract."""

    def __init__(self, settings: AppSettings, docker_client: Any = None) -> None:
        self._settings = settings
        self._client = docker_client

    def _api(self):
        if self._client is None:
            self._client = docker.from_env()
        return self._client

    async def validate(self, candidate: dict[str, Any]) -> dict[str, Any]:
        """Return ``{"status": "valid"|"invalid", "image_id", "digest",
        "message"}``. Raises ``BackendUnavailable`` when the daemon/registry is
        unreachable (the profile service maps that to a 503)."""
        return await asyncio.to_thread(self._validate_blocking, candidate)

    def _validate_blocking(self, candidate: dict[str, Any]) -> dict[str, Any]:
        image = str(candidate.get("image") or "").strip()
        pull_policy = str(candidate.get("pull_policy") or "if_not_present")
        capabilities = set(candidate.get("runtime_capabilities") or [])
        keepalive = list(candidate.get("keepalive_command") or ["tail", "-f", "/dev/null"])

        try:
            self._ensure_image(image, pull_policy)
        except docker.errors.ImageNotFound as exc:
            return {"status": "invalid", "image_id": None, "digest": None, "message": sanitize_error_message(str(exc))}
        except docker.errors.NotFound as exc:
            return {"status": "invalid", "image_id": None, "digest": None, "message": sanitize_error_message(str(exc))}
        except (docker.errors.APIError, docker.errors.DockerException, ConnectionError, OSError) as exc:
            raise BackendUnavailable(
                f"image validation unavailable: {sanitize_error_message(str(exc))}", cause=exc
            ) from exc

        image_id, digest = self._image_identity(image)

        container = None
        try:
            container = self._api().containers.run(
                image=image,
                command=keepalive,
                detach=True,
                entrypoint=[],
                network_mode="none",
                mem_limit=str(candidate.get("memory_limit") or "512m"),
                pids_limit=int(candidate.get("pids_limit") or 256),
                nano_cpus=int(float(candidate.get("cpus") or 1.0) * 1e9),
                tmpfs={"/tmp": f"size={candidate.get('tmpfs_size') or '128m'}"},
                labels={_VALIDATION_LABEL: "1"},
            )
            try:
                container.reload()
            except Exception:
                pass
            if container.status != "running":
                return self._invalid(image_id, digest, f"keepalive command did not keep the container running (status: {container.status})")

            for probe, message in self._contract_probes(container, capabilities):
                if probe.exit_code != 0:
                    return self._invalid(image_id, digest, message)
            return {
                "status": "valid",
                "image_id": image_id,
                "digest": digest,
                "message": "probes passed",
            }
        except BackendUnavailable:
            raise
        except (docker.errors.APIError, docker.errors.DockerException, ConnectionError, OSError) as exc:
            raise BackendUnavailable(
                f"image validation unavailable: {sanitize_error_message(str(exc))}", cause=exc
            ) from exc
        except Exception as exc:
            return self._invalid(image_id, digest, sanitize_error_message(str(exc)))
        finally:
            if container is not None:
                self._remove_validation_container(container)

    def _invalid(self, image_id: str | None, digest: str | None, message: str) -> dict[str, Any]:
        return {"status": "invalid", "image_id": image_id, "digest": digest, "message": message}

    def _ensure_image(self, image: str, pull_policy: str) -> None:
        client = self._api()
        if pull_policy == "never":
            client.images.get(image)  # raises ImageNotFound if absent
            return
        if pull_policy == "always":
            client.images.pull(image)
            return
        # if_not_present
        try:
            client.images.get(image)
        except (docker.errors.ImageNotFound, docker.errors.NotFound):
            client.images.pull(image)

    def _image_identity(self, image: str) -> tuple[str | None, str | None]:
        try:
            image_attrs = self._api().images.get(image).attrs
        except Exception:
            return None, None
        image_id = image_attrs.get("Id") if isinstance(image_attrs, dict) else None
        digests = image_attrs.get("RepoDigests") if isinstance(image_attrs, dict) else None
        digest = None
        if isinstance(digests, list):
            for ref in digests:
                if isinstance(ref, str) and "@" in ref:
                    digest = ref.rsplit("@", 1)[-1]
                    break
        return image_id, digest

    def _contract_probes(self, container, capabilities: set[str]) -> list[tuple[Any, str]]:
        probes: list[tuple[Any, str]] = [
            (container.exec_run(["/bin/sh", "-c", "true"]), "container has no usable /bin/sh"),
        ]
        for capability in sorted(capabilities):
            if capability == "shell":
                continue
            binary = {"python": "python", "nodejs": "node"}.get(capability)
            if binary is None:
                continue
            probes.append(
                (container.exec_run([binary, "--version"]), f"declared runtime binary '{binary}' is not runnable")
            )
            runner = _RUNNER_FILES.get(capability)
            if runner:
                probes.append(
                    (container.exec_run(["test", "-f", runner]), f"declared runtime runner file '{runner}' is missing")
                )
        return probes

    def _remove_validation_container(self, container) -> None:
        try:
            container.stop(timeout=5)
        except Exception:
            pass
        try:
            container.remove(force=True)
        except Exception:
            pass
