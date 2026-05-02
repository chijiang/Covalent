from __future__ import annotations

import fnmatch
import logging
import os

from agent_framework.skills.spec import ManifestSkillSpec

logger = logging.getLogger(__name__)

_ALLOW_ALL_SYSTEM_ENV = {
    "PATH", "HOME", "USER", "TMPDIR", "LANG", "TERM",
    "VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME",
    "LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH",
    "NODE_PATH",
    "SHELL", "LOGNAME", "PWD",
}


class PermissionChecker:
    """Validates skill actions against declared permissions in the manifest.

    Runtime enforcement layers:
    - env vars: filtered at subprocess spawn (hard limit)
    - filesystem: injected as SKILL_FS_{READ,WRITE} env vars for SDK-side checks
    - network: injected as SKILL_NET_{ALLOW,DENY} env vars for SDK-side checks
    - subprocess: SKILL_ALLOW_SUBPROCESS env var for SDK-side checks
    """

    @staticmethod
    def check_filesystem_read(spec: ManifestSkillSpec, path: str) -> bool:
        prefixes = _expand_skill_dir(spec, spec.permissions.filesystem.read)
        if not prefixes:
            return True
        return any(path.startswith(p) for p in prefixes)

    @staticmethod
    def check_filesystem_write(spec: ManifestSkillSpec, path: str) -> bool:
        prefixes = _expand_skill_dir(spec, spec.permissions.filesystem.write)
        if not prefixes:
            return False
        return any(path.startswith(p) for p in prefixes)

    @staticmethod
    def check_network(spec: ManifestSkillSpec, host: str) -> bool:
        for pattern in spec.permissions.network.deny:
            if fnmatch.fnmatch(host, pattern):
                return False
        if spec.permissions.network.allow_outbound:
            for pattern in spec.permissions.network.allow_outbound:
                if fnmatch.fnmatch(host, pattern):
                    return True
            return False
        return True

    @staticmethod
    def check_subprocess(spec: ManifestSkillSpec) -> bool:
        return spec.permissions.subprocess

    @staticmethod
    def filter_env(spec: ManifestSkillSpec, host_env: dict[str, str]) -> dict[str, str]:
        allowed_vars = set(spec.permissions.env_vars) | _ALLOW_ALL_SYSTEM_ENV
        filtered: dict[str, str] = {}
        for key, value in host_env.items():
            if key.startswith("SKILL_") or key in allowed_vars:
                filtered[key] = value
        return filtered

    @staticmethod
    def inject_permission_env(spec: ManifestSkillSpec, env: dict[str, str]) -> dict[str, str]:
        """Inject permission declarations as env vars so the SDK can enforce them.

        These are consumed by the skill SDK to do runtime checks before performing
        filesystem, network, or subprocess operations.
        """
        env["SKILL_FS_READ"] = os.pathsep.join(
            _expand_skill_dir(spec, spec.permissions.filesystem.read)
        )
        env["SKILL_FS_WRITE"] = os.pathsep.join(
            _expand_skill_dir(spec, spec.permissions.filesystem.write)
        )
        env["SKILL_NET_ALLOW"] = ",".join(spec.permissions.network.allow_outbound)
        env["SKILL_NET_DENY"] = ",".join(spec.permissions.network.deny)
        env["SKILL_ALLOW_SUBPROCESS"] = "1" if spec.permissions.subprocess else "0"
        return env

    @staticmethod
    def log_permission_summary(spec: ManifestSkillSpec) -> None:
        """Log a summary of the effective permissions for audit."""
        fs_read = spec.permissions.filesystem.read or ["(unrestricted)"]
        fs_write = spec.permissions.filesystem.write or ["(none)"]
        net = spec.permissions.network.allow_outbound or ["(unrestricted)"]
        logger.info(
            "Skill '%s' permissions — fs.read: %s, fs.write: %s, net: %s, subprocess: %s",
            spec.name,
            fs_read,
            fs_write,
            net,
            spec.permissions.subprocess,
        )


def _expand_skill_dir(spec: ManifestSkillSpec, paths: list[str]) -> list[str]:
    return [p.replace("${SKILL_DIR}", spec.source_dir or "") for p in paths]
