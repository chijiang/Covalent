from __future__ import annotations


class SkillError(Exception):
    """Base exception for all skill-related errors."""


class SkillLoadError(SkillError):
    """Raised when a skill manifest cannot be parsed or validated."""


class SkillProcessError(SkillError):
    """Raised when a skill subprocess returns a JSON-RPC error response."""

    def __init__(self, error: dict[str, object]) -> None:
        self.code: int = error.get("code", -32002)  # type: ignore[assignment]
        self.message: str = error.get("message", "Unknown skill process error")  # type: ignore[assignment]
        self.data = error.get("data")
        super().__init__(f"Skill process error [{self.code}]: {self.message}")


class SkillStartupError(SkillError):
    """Raised when a skill subprocess fails to become ready within the startup timeout."""
