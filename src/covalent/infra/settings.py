from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


MANAGED_SKILL_LOCAL_CATEGORIES = ("built_in", "uploaded", "authored")
MANAGED_SKILL_ALL_CATEGORIES = (*MANAGED_SKILL_LOCAL_CATEGORIES, "github_synced")

# Default secrets shipped with the source. They exist so a fresh `dev` checkout
# works with zero configuration; any non-dev deployment MUST override them.
# `AppSettings.validate_runtime_secrets` refuses to start if they are still in
# place (or empty) outside dev mode.
DEFAULT_API_TOKEN_HASH_PEPPER = "dev-token-pepper-change-me"
DEFAULT_CONSOLE_SESSION_SECRET = "dev-session-secret-change-me"
DEFAULT_SEED_ADMIN_PASSWORD = "admin123"


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
    # Max simultaneous in-flight public agent runs (streaming + non-streaming)
    # per API token. 0 = unlimited (back-compat). Excess requests get 429.
    api_token_max_concurrent_runs: int = 4
    api_token_hash_pepper: str = DEFAULT_API_TOKEN_HASH_PEPPER
    console_auth_mode: str = "local"
    console_auth_jwt_secret: str | None = None
    console_auth_jwt_issuer: str | None = None
    console_auth_jwt_audience: str | None = None
    # Optional HMAC secret for trusted_header mode. When set, the reverse proxy
    # must sign a digest of the identity headers (see _verify_trusted_header_signature)
    # and present it as `x-covalent-signature`. This prevents a client that
    # bypasses the proxy from forging admin identity via raw headers.
    # When unset, trusted_header mode trusts the headers verbatim (back-compat);
    # validate_runtime_secrets logs a warning in that case outside dev mode.
    console_trusted_header_secret: str | None = None
    console_session_secret: str = DEFAULT_CONSOLE_SESSION_SECRET
    console_session_cookie_name: str = "covalent_console_session"
    console_session_cookie_secure: bool | None = None
    console_session_max_age_seconds: int = 60 * 60 * 24 * 14
    console_signup_enabled: bool = True
    console_seed_admin_enabled: bool = True
    console_seed_admin_username: str = "admin"
    console_seed_admin_email: str = "admin@local"
    console_seed_admin_password: str = DEFAULT_SEED_ADMIN_PASSWORD
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
    # Where skills run. "filesystem" = local subprocesses with NO OS-level
    # isolation (only safe for trusted skills; PermissionGuard is best-effort).
    # "docker" = per-session container (use for untrusted/third-party skills).
    execution_backend_kind: Literal["filesystem", "docker", "kubernetes"] = "filesystem"
    execution_backend_docker_image: str = "covalent-sandbox:dev"
    execution_backend_docker_mem_limit: str = "512m"
    execution_backend_docker_pids_limit: int = 256
    execution_backend_docker_cpus: float = 1.0
    execution_backend_docker_network: Literal["none", "bridge"] = "none"
    execution_backend_docker_tmpfs_size: str = "128m"
    execution_backend_docker_reaper_interval_seconds: float = 60.0
    # 0 = unlimited; >0 = queue new sessions when at capacity (asyncio.Semaphore).
    execution_backend_docker_max_sessions: int = 0
    # Idle timeout: stop a sandbox container if no skill/script/shell activity
    # for this many seconds. 0 = never auto-stop (session DELETE / reaper only).
    execution_backend_docker_idle_timeout_seconds: float = 1800.0
    # Shell tool: an opt-in, sandbox-only tool that runs a shell command in the
    # session's container. Never registered on the filesystem backend.
    execution_backend_shell_tool_enabled: bool = False
    execution_backend_shell_tool_binary: str = "sh"
    execution_backend_shell_tool_timeout_seconds: float = 120.0
    execution_backend_shell_tool_max_bytes: int = 51200
    skills_root_dir: str = "skills"
    skills_directories: str | None = None
    skills_cache_dir: str = "~/.covalent/skill_cache"

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
        return self.workspace_root() / ".covalent" / "session-workspaces"

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

    def is_dev_auth_mode(self) -> bool:
        return (self.console_auth_mode or "local").strip().lower() == "dev"

    def validate_runtime_secrets(self) -> None:
        """Refuse to start outside dev auth mode if any secret is still the
        shipped default or empty. Dev mode is meant for a zero-config local
        checkout and is exempt.

        Raises ``RuntimeError`` listing every offending setting so the operator
        sees all required changes in one shot.
        """
        if self.is_dev_auth_mode():
            return

        problems: list[str] = []
        if not self.api_token_hash_pepper or self.api_token_hash_pepper == DEFAULT_API_TOKEN_HASH_PEPPER:
            problems.append(
                "AGENT_FRAMEWORK_API_TOKEN_HASH_PEPPER must be set to a non-default value"
            )
        if (
            not self.console_session_secret
            or self.console_session_secret == DEFAULT_CONSOLE_SESSION_SECRET
        ):
            problems.append(
                "AGENT_FRAMEWORK_CONSOLE_SESSION_SECRET must be set to a non-default value"
            )
        if (
            self.console_seed_admin_enabled
            and (
                not self.console_seed_admin_password
                or self.console_seed_admin_password == DEFAULT_SEED_ADMIN_PASSWORD
            )
        ):
            problems.append(
                "AGENT_FRAMEWORK_CONSOLE_SEED_ADMIN_PASSWORD must be set to a non-default value"
                " (or disable CONSOLE_SEED_ADMIN_ENABLED)"
            )

        if problems:
            raise RuntimeError(
                "Refusing to start with insecure default secrets outside dev auth mode:\n  - "
                + "\n  - ".join(problems)
            )
