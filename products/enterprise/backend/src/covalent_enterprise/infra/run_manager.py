"""Enterprise composition for durable runs; preserves the session-factory API."""
from covalent_runtime.services.run_manager import (
    RunManager as RuntimeRunManager,
    RunManagerError as RunManagerError,
    RUN_STATUS_RUNNING as RUN_STATUS_RUNNING,
    RUN_STATUS_CANCELLING as RUN_STATUS_CANCELLING,
    RUN_STATUS_COMPLETED as RUN_STATUS_COMPLETED,
    RUN_STATUS_CANCELLED as RUN_STATUS_CANCELLED,
    RUN_STATUS_FAILED as RUN_STATUS_FAILED,
    RUN_TERMINAL_EVENTS as RUN_TERMINAL_EVENTS,
    _group_reasoning_fragments as _group_reasoning_fragments,
)
from covalent_enterprise.infra.run_store import PostgresRunStore


class RunManager(RuntimeRunManager):
    def __init__(self, session_factory) -> None:
        super().__init__(PostgresRunStore(session_factory))
