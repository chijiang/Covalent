from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class SkillSpec(BaseModel):
    name: str
    description: str
    instructions: str
    tools: list[str] = Field(default_factory=list)
    prompts: list[str] = Field(default_factory=list)
    resources: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


# --- Manifest-based skill types ---


class ToolDeclaration(BaseModel):
    name: str
    description: str
    handler: str | None = None
    parameters: dict[str, Any] = Field(default_factory=lambda: {"type": "object", "properties": {}})

    def to_openai_tool_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ScriptDeclaration(BaseModel):
    name: str
    path: str
    description: str = ""
    runtime: Literal["python", "nodejs", "bash"] | None = None
    timeout_seconds: float = 60.0


class NetworkPermission(BaseModel):
    allow_outbound: list[str] = Field(default_factory=list)
    deny: list[str] = Field(default_factory=list)


class FilesystemPermission(BaseModel):
    read: list[str] = Field(default_factory=list)
    write: list[str] = Field(default_factory=list)


class Permissions(BaseModel):
    network: NetworkPermission = Field(default_factory=NetworkPermission)
    filesystem: FilesystemPermission = Field(default_factory=FilesystemPermission)
    env_vars: list[str] = Field(default_factory=list)
    subprocess: bool = False


class ProcessConfig(BaseModel):
    max_instances: int = 1
    idle_timeout_seconds: float = 300.0
    startup_timeout_seconds: float = 15.0
    max_request_timeout_seconds: float = 60.0


class HealthCheckConfig(BaseModel):
    interval_seconds: float = 30.0
    max_failures: int = 3


class SkillRuntime(BaseModel):
    type: Literal["python", "nodejs"]
    protocol: Literal["rpc", "callable"] = "rpc"
    entry_point: str
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    working_dir: str | None = None


class ManifestSkillSpec(BaseModel):
    name: str
    version: str = "0.1.0"
    description: str
    author: str = ""
    tags: list[str] = Field(default_factory=list)
    runtime: SkillRuntime | None = None
    tools: list[ToolDeclaration] = Field(default_factory=list)
    scripts: list[ScriptDeclaration] = Field(default_factory=list)
    references: list[str] = Field(default_factory=list)
    instructions: str = ""
    resource_files: list[str] = Field(default_factory=list)
    eager_resource_files: list[str] = Field(default_factory=list)
    permissions: Permissions = Field(default_factory=Permissions)
    process: ProcessConfig = Field(default_factory=ProcessConfig)
    health_check: HealthCheckConfig = Field(default_factory=HealthCheckConfig)
    source_dir: str | None = None
    source_type: Literal["local", "git"] = "local"
    git_url: str | None = None
    git_ref: str | None = None

    def to_skill_spec(self) -> SkillSpec:
        return SkillSpec(
            name=self.name,
            description=self.description,
            instructions=self.instructions,
            tools=self.references + [t.name for t in self.tools],
            metadata={"version": self.version, "source_type": self.source_type},
        )

    def resolved_entry_point(self) -> str:
        import os

        assert self.runtime is not None, f"Skill '{self.name}' has no runtime"
        assert self.source_dir is not None, f"Skill '{self.name}' has no source_dir"
        return os.path.abspath(os.path.join(self.source_dir, self.runtime.entry_point))

    def resolved_working_dir(self) -> str:
        import os

        if self.runtime is None:
            return os.path.abspath(self.source_dir or ".")
        return os.path.abspath(self.runtime.working_dir or self.source_dir or ".")

    @property
    def is_executable(self) -> bool:
        return self.runtime is not None and bool(self.runtime.entry_point)

    @property
    def has_bundle_tools(self) -> bool:
        return bool(self.scripts or self.resource_files)
