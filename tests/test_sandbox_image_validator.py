"""Tests for the sandbox image validator (fake Docker client, no daemon)."""

from __future__ import annotations

import asyncio
import types
import unittest

import docker

from covalent.infra.settings import AppSettings
from covalent.runtime.backend import BackendUnavailable
from covalent.runtime.sandbox_image_validator import DockerImageValidator, sanitize_error_message


def _candidate(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "image": "covalent-sandbox:py312",
        "pull_policy": "if_not_present",
        "keepalive_command": ["tail", "-f", "/dev/null"],
        "runtime_capabilities": ["python", "shell"],
        "contract_version": 1,
        "memory_limit": "512m",
        "pids_limit": 256,
        "cpus": 1.0,
        "tmpfs_size": "128m",
    }
    payload.update(overrides)
    return payload


class _FakeImage:
    def __init__(self, attrs: dict | None = None) -> None:
        self.attrs = attrs or {
            "Id": "sha256:fakeimageid",
            "RepoDigests": ["covalent-sandbox@sha256:abcdef"],
        }


class _FakeImages:
    def __init__(self) -> None:
        self._images: dict[str, _FakeImage] = {}
        self.pulls: list[str] = []
        self.gets: list[str] = []

    def get(self, name: str) -> _FakeImage:
        self.gets.append(name)
        image = self._images.get(name)
        if image is None:
            raise docker.errors.ImageNotFound(f"No such image: {name}")
        return image

    def pull(self, name: str) -> _FakeImage:
        self.pulls.append(name)
        image = _FakeImage()
        self._images[name] = image
        return image

    def add(self, name: str, attrs: dict | None = None) -> None:
        self._images[name] = _FakeImage(attrs)


class _FakeContainer:
    def __init__(self, *, shell_ok: bool = True, binaries_ok: bool = True, runner_ok: bool = True) -> None:
        self.status = "running"
        self.removed = False
        self._shell_ok = shell_ok
        self._binaries_ok = binaries_ok
        self._runner_ok = runner_ok

    def reload(self) -> None:
        pass

    def stop(self, **_kw) -> None:
        self.status = "exited"

    def remove(self, **_kw) -> None:
        self.removed = True

    def exec_run(self, cmd: list[str], **_kw):
        command = list(cmd)
        if command[0] == "/bin/sh":
            return types.SimpleNamespace(exit_code=0 if self._shell_ok else 127, output=(b"", b""))
        if command[0] == "test":
            # "test -f <path>" — runner file existence probe.
            return types.SimpleNamespace(exit_code=0 if self._runner_ok else 1, output=(b"", b""))
        if command[0] in ("python", "node"):
            return types.SimpleNamespace(exit_code=0 if self._binaries_ok else 127, output=(b"", b""))
        return types.SimpleNamespace(exit_code=0, output=(b"", b""))


class _FakeContainers:
    def __init__(self, **container_kwargs) -> None:
        self._container_kwargs = container_kwargs
        self.run_calls: list[dict] = []

    def run(self, **kwargs) -> _FakeContainer:
        self.run_calls.append(kwargs)
        return _FakeContainer(**self._container_kwargs)

    def get(self, _name):
        raise docker.errors.NotFound("no such container")


class _FakeDockerClient(types.SimpleNamespace):
    def __init__(self, *, images=None, containers=None) -> None:
        super().__init__(images=images or _FakeImages(), containers=containers or _FakeContainers())


class DockerImageValidatorTests(unittest.IsolatedAsyncioTestCase):
    def _validator(self, client) -> DockerImageValidator:
        return DockerImageValidator(AppSettings(), docker_client=client)

    async def test_validates_python_image_successfully(self) -> None:
        images = _FakeImages()
        images.add("covalent-sandbox:py312")
        client = _FakeDockerClient(images=images)
        validator = self._validator(client)

        result = await validator.validate(_candidate())

        self.assertEqual(result["status"], "valid")
        self.assertEqual(result["image_id"], "sha256:fakeimageid")
        self.assertEqual(result["digest"], "sha256:abcdef")
        self.assertEqual(client.containers.run_calls[0]["network_mode"], "none")
        self.assertEqual(client.containers.run_calls[0]["entrypoint"], [])

    async def test_fails_when_runner_file_missing(self) -> None:
        images = _FakeImages()
        images.add("covalent-sandbox:py312")
        client = _FakeDockerClient(images=images, containers=_FakeContainers(runner_ok=False))
        validator = self._validator(client)

        result = await validator.validate(_candidate())

        self.assertEqual(result["status"], "invalid")
        self.assertIn("runner", result["message"].lower())

    async def test_fails_when_python_binary_missing(self) -> None:
        images = _FakeImages()
        images.add("covalent-sandbox:py312")
        client = _FakeDockerClient(images=images, containers=_FakeContainers(binaries_ok=False))
        validator = self._validator(client)

        result = await validator.validate(_candidate())

        self.assertEqual(result["status"], "invalid")
        self.assertIn("python", result["message"].lower())

    async def test_pull_policy_never_requires_local_image(self) -> None:
        images = _FakeImages()
        client = _FakeDockerClient(images=images)
        validator = self._validator(client)

        result = await validator.validate(_candidate(pull_policy="never"))

        self.assertEqual(result["status"], "invalid")
        self.assertEqual(images.pulls, [], "pull_policy=never must not pull")
        self.assertIn("no such image", result["message"].lower())

    async def test_pull_policy_if_not_present_pulls_when_missing(self) -> None:
        images = _FakeImages()
        client = _FakeDockerClient(images=images)
        validator = self._validator(client)

        await validator.validate(_candidate(pull_policy="if_not_present"))

        self.assertEqual(images.pulls, ["covalent-sandbox:py312"])

    async def test_pull_policy_always_pulls_even_when_present(self) -> None:
        images = _FakeImages()
        images.add("covalent-sandbox:py312")
        client = _FakeDockerClient(images=images)
        validator = self._validator(client)

        await validator.validate(_candidate(pull_policy="always"))

        self.assertEqual(images.pulls, ["covalent-sandbox:py312"])

    async def test_daemon_down_raises_backend_unavailable(self) -> None:
        class _DownImages:
            def get(self, _name):
                raise docker.errors.APIError("daemon down")

            def pull(self, _name):
                raise docker.errors.APIError("daemon down")

        client = _FakeDockerClient(images=_DownImages())
        validator = self._validator(client)

        with self.assertRaises(BackendUnavailable):
            await validator.validate(_candidate())


class SanitizerTests(unittest.TestCase):
    def test_redacts_credential_patterns(self) -> None:
        cleaned = sanitize_error_message(
            "pull failed: authorization: hunter2 (bearer abc.def.ghi) and password=s3cret tail"
        )
        self.assertNotIn("hunter2", cleaned)
        self.assertNotIn("abc.def.ghi", cleaned)
        self.assertNotIn("s3cret", cleaned)
        self.assertIn("<redacted>", cleaned)

    def test_bearer_pattern_does_not_crash(self) -> None:
        # Regression: the bearer pattern has no capture group beyond the label;
        # sanitizing a bearer-bearing message must not raise.
        cleaned = sanitize_error_message("registry denied: Bearer eyJhbGciOi.payload.sig")
        self.assertIn("Bearer <redacted>", cleaned)

    def test_caps_message_length(self) -> None:
        cleaned = sanitize_error_message("x" * 10_000)
        self.assertLessEqual(len(cleaned), 500)


if __name__ == "__main__":
    unittest.main()
