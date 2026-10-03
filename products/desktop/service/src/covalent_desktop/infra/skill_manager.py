"""Managed Desktop skill sources and safe archive extraction."""

from __future__ import annotations

import base64
import io
import re
import shutil
import tempfile
import zipfile
from pathlib import Path

from covalent_agent_kit.skills.loader import SkillLoader
from covalent_agent_kit.skills.exceptions import SkillLoadError


class DesktopSkillManager:
    def __init__(self, loader: SkillLoader, root: Path) -> None:
        self.loader = loader
        self.root = root

    def create_authored(self, name: str, content: str) -> str:
        if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_-]{0,63}", name):
            raise ValueError("Invalid skill name")
        if not content.strip() or len(content.encode()) > 200_000:
            raise ValueError("Skill instructions must contain 1–200000 bytes")
        path = self.root / "authored" / name
        path.mkdir(parents=True, exist_ok=True)
        file = path / "SKILL.md"
        previous = file.read_text(encoding="utf-8") if file.exists() else None
        file.write_text(content, encoding="utf-8")
        try:
            spec = self.loader.load_skill_dir(path)
            if spec.name != name:
                raise ValueError("Skill metadata name must match its folder name")
        except Exception as error:
            if previous is None:
                shutil.rmtree(path)
            else:
                file.write_text(previous, encoding="utf-8")
            raise ValueError(str(error)) from error
        return spec.name

    def install_zip(self, name: str, encoded: str) -> str:
        if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_-]{0,63}", name):
            raise ValueError("Invalid skill name")
        archive = base64.b64decode(encoded, validate=True)
        if len(archive) > 10_000_000:
            raise ValueError("Skill archive exceeds 10 MB")
        target = self.root / "uploaded" / name
        if target.exists():
            raise ValueError("A skill with this upload name already exists")
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=target.parent) as directory:
            staging = Path(directory)
            with zipfile.ZipFile(io.BytesIO(archive)) as source:
                entries = source.infolist()
                if (
                    len(entries) > 1000
                    or sum(item.file_size for item in entries) > 50_000_000
                ):
                    raise ValueError("Skill archive exceeds extraction limits")
                for item in entries:
                    parts = Path(item.filename).parts
                    if (
                        item.filename.startswith("/")
                        or ".." in parts
                        or (item.external_attr >> 16) & 0o170000 == 0o120000
                    ):
                        raise ValueError("Skill archive contains an unsafe path")
                    output = staging.joinpath(*parts)
                    if item.is_dir():
                        output.mkdir(parents=True, exist_ok=True)
                    else:
                        output.parent.mkdir(parents=True, exist_ok=True)
                        with source.open(item) as inp, output.open("wb") as out:
                            shutil.copyfileobj(inp, out)
            candidates = [path.parent for path in staging.rglob("SKILL.md")]
            candidates += [path.parent for path in staging.rglob("skill.yaml")]
            candidates = list(dict.fromkeys(candidates))
            if len(candidates) != 1:
                raise ValueError("Archive must contain exactly one skill")
            try:
                spec = self.loader.load_skill_dir(candidates[0])
            except SkillLoadError as error:
                raise ValueError(str(error)) from error
            shutil.move(str(candidates[0]), str(target))
            return spec.name

    def delete(self, name: str) -> None:
        for spec in self.loader.discover_local():
            if spec.name != name or not spec.source_dir:
                continue
            path = Path(spec.source_dir).resolve()
            if path.parent in {
                (self.root / "uploaded").resolve(),
                (self.root / "authored").resolve(),
            }:
                shutil.rmtree(path)
                return
        raise ValueError("Only authored and uploaded skills can be removed")

    async def sync_git(
        self, name: str, url: str, ref: str | None = None, subdir: str | None = None
    ) -> list[str]:
        if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_-]{0,63}", name):
            raise ValueError("Invalid Git source name")
        if not url.startswith("https://"):
            raise ValueError("Git skill source must use HTTPS")
        if subdir and (Path(subdir).is_absolute() or ".." in Path(subdir).parts):
            raise ValueError("Invalid Git skill subdirectory")
        specs = await self.loader.discover_git(
            [{"name": name, "url": url, "ref": ref, "subdir": subdir}]
        )
        return [spec.name for spec in specs]
