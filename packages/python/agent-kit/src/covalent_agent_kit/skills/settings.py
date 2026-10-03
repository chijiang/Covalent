from pathlib import Path
from typing import Protocol

class SkillSettings(Protocol):
    skills_directories: str | None
    def local_skill_directories(self) -> list[Path]: ...
    def managed_skill_directory(self, category: str) -> Path: ...
