from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


MANAGED_SKILL_LOCAL_CATEGORIES = ("built_in", "uploaded", "authored")
MANAGED_SKILL_ALL_CATEGORIES = (*MANAGED_SKILL_LOCAL_CATEGORIES, "github_synced")


class AppSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AGENT_FRAMEWORK_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "Covalent"
    database_url: str | None = None
    default_provider: str = "openai_compatible"
    default_model: str = "gpt-4o-mini"
    default_base_url: str = "https://api.openai.com/v1"
    default_api_key: str | None = None
    request_timeout_seconds: float = 500.0
    default_max_iterations: int = 10
    session_history_limit: int = 40
    context_token_budget: int | None = None
    context_compact_threshold: float = 0.75
    context_summary_model: str | None = None
    enable_llm_summarization: bool = True
    enable_builtin_tools: bool = True
    mcp_enabled: bool = True
    mcp_timeout_seconds: float = 500.0
    mcp_servers_json: str | None = None
    max_upload_bytes: int = 100 * 1024 * 1024  # 100 MB
    api_token_hash_pepper: str = "dev-token-pepper-change-me"
    console_auth_mode: str = "local"
    console_auth_jwt_secret: str | None = None
    console_auth_jwt_issuer: str | None = None
    console_auth_jwt_audience: str | None = None
    console_session_secret: str = "dev-session-secret-change-me"
    console_session_cookie_name: str = "covalent_console_session"
    console_session_max_age_seconds: int = 60 * 60 * 24 * 14
    console_signup_enabled: bool = True
    console_seed_admin_enabled: bool = True
    console_seed_admin_username: str = "admin"
    console_seed_admin_password: str = "admin123"
    console_seed_admin_display_name: str = "Admin"
    console_seed_admin_workspace_name: str = "Default workspace"
    agents_json: str | None = None
    skill_sources_json: str | None = None
    agent_system_prompt: str = (
        "You are a general-purpose ReAct assistant. Help the user by understanding the goal, "
        "using available tools or delegates only when they improve accuracy or reduce uncertainty, "
        "and providing clear, grounded final answers."
    )
    agent_description: str = "General-purpose ReAct agent"
    reasoning_skill_name: str = "general_reasoning"
    reasoning_skill_description: str = "Base reasoning and tool usage skill"
    reasoning_skill_instructions: str = (
        "Use a ReAct loop when it helps: understand the task, decide whether the current context is sufficient, "
        "use the most relevant tool or delegate only when it reduces uncertainty, incorporate observations, "
        "repeat only as needed, and stop once you can answer confidently. Keep the final response clear, direct, "
        "and grounded in the evidence you observed."
    )
    workspace_root_dir: str = "."
    session_workspace_enabled: bool = True
    session_workspace_root_dir: str | None = None
    skills_root_dir: str = "skills"
    skills_directories: str | None = None
    skills_cache_dir: str = "~/.agent_framework/skill_cache"

    def resolve_path(self, path: str | None) -> Path | None:
        return Path(path).expanduser() if path else None

    def managed_skills_root(self) -> Path:
        return Path(self.skills_root_dir).expanduser()

    def workspace_root(self) -> Path:
        return Path(self.workspace_root_dir).expanduser().resolve()

    def session_workspace_root(self) -> Path:
        """Get the root directory for session-scoped workspaces."""
        if self.session_workspace_root_dir:
            return Path(self.session_workspace_root_dir).expanduser().resolve()
        return self.workspace_root() / ".agent_framework" / "session-workspaces"

    def session_workspace_dir(self, session_id: str) -> Path:
        """Get the workspace directory for a specific session."""
        if not self.session_workspace_enabled:
            return self.workspace_root()
        safe_id = "".join(char if char.isalnum() or char in "._-" else "-" for char in session_id.strip())
        safe_id = safe_id.strip(".-") or "unknown"
        return self.session_workspace_root() / safe_id

    def managed_skill_directory(self, category: str) -> Path:
        if category not in MANAGED_SKILL_ALL_CATEGORIES:
            raise ValueError(f"Unknown managed skill category: {category}")
        return self.managed_skills_root() / category

    def local_skill_directories(self) -> list[Path]:
        if self.skills_directories:
            results: list[Path] = []
            for raw in self.skills_directories.split(":"):
                resolved = self.resolve_path(raw.strip())
                if resolved is not None:
                    results.append(resolved)
            return results
        return [self.managed_skill_directory(category) for category in MANAGED_SKILL_LOCAL_CATEGORIES]

    def ensure_managed_skill_directories(self) -> None:
        root = self.managed_skills_root()
        root.mkdir(parents=True, exist_ok=True)
        for category in MANAGED_SKILL_ALL_CATEGORIES:
            (root / category).mkdir(parents=True, exist_ok=True)
