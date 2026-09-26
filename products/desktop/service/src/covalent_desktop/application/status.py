"""Service status use case shared by local transport adapters."""

from __future__ import annotations

from dataclasses import asdict, dataclass

from covalent_desktop import __version__


PROTOCOL_VERSION = 1


@dataclass(frozen=True, slots=True)
class ServiceStatus:
    status: str
    service_version: str
    protocol_version: int
    capabilities: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["capabilities"] = list(self.capabilities)
        return value


def get_service_status() -> ServiceStatus:
    return ServiceStatus(
        status="ok",
        service_version=__version__,
        protocol_version=PROTOCOL_VERSION,
        capabilities=("health", "agents", "chat"),
    )
