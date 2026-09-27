"""Sync bundled built-in skills into the user data directory."""

from __future__ import annotations

import logging
import os
import shutil
import sys
from pathlib import Path

LOGGER = logging.getLogger(__name__)
ENV_SOURCE = "COVALENT_DESKTOP_BUILT_IN_SKILLS"
FROZEN_DIR_NAME = "built_in_skills"
SKILL_MARKERS = ("SKILL.md", "skill.yaml")


def bundled_built_in_skills_root() -> Path | None:
    """Locate the shipped built-in skills, or None when nothing is bundled.

    Resolution order: explicit env override, PyInstaller bundle, repository
    checkout (products/desktop/service/src/covalent_desktop/infra -> repo root).
    """
    override = os.environ.get(ENV_SOURCE)
    if override:
        path = Path(override)
        return path if path.is_dir() else None
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        path = Path(meipass) / FROZEN_DIR_NAME
        if path.is_dir():
            return path
    path = Path(__file__).resolve().parents[6] / "skills" / "built_in"
    return path if path.is_dir() else None


def sync_built_in_skills(source: Path, target: Path) -> list[str]:
    """Mirror bundled skill directories into target and prune stale ones.

    Built-in skills are product assets and read-only through the API, so every
    startup overwrites the data-directory copy; that keeps upgrades propagating
    without per-file bookkeeping.
    """
    if source.resolve() == target.resolve():
        raise ValueError("Built-in skills source and target must differ")
    sources = {
        entry.name: entry
        for entry in sorted(source.iterdir())
        if entry.is_dir() and any((entry / marker).is_file() for marker in SKILL_MARKERS)
    }
    target.mkdir(parents=True, exist_ok=True)
    for name, origin in sources.items():
        destination = target / name
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(origin, destination)
    for entry in target.iterdir():
        if entry.is_dir() and entry.name not in sources:
            LOGGER.info("Removing stale built-in skill %s", entry.name)
            shutil.rmtree(entry)
    return sorted(sources)


def sync_bundled_skills(target: Path) -> list[str]:
    """Sync the bundled built-in skills into target; [] when none are bundled."""
    source = bundled_built_in_skills_root()
    if source is None:
        LOGGER.debug("No bundled built-in skills found")
        return []
    return sync_built_in_skills(source, target)
