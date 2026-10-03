from pathlib import Path
from typing import Literal, Protocol

class ExecutionSettings(Protocol):
    execution_backend_docker_cpus: float
    execution_backend_docker_idle_timeout_seconds: float
    execution_backend_docker_image: str
    execution_backend_docker_max_instances: int
    execution_backend_docker_max_sessions: int
    execution_backend_docker_mem_limit: str
    execution_backend_docker_network: Literal['none', 'bridge']
    execution_backend_docker_pids_limit: int
    execution_backend_docker_reaper_interval_seconds: float
    execution_backend_docker_tmpfs_size: str

    def workspace_root(self) -> Path: ...
    def session_workspace_dir(self, session_id: str) -> Path: ...
