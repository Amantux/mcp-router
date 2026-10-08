"""FR-01 discovery: registration, config import, catalog sync, health, loop."""

from mcprouter.discovery.health import HealthPolicy, HealthTracker, next_status
from mcprouter.discovery.loop import SyncLoop
from mcprouter.discovery.registry import (
    DuplicateServerError,
    InvalidRegistrationError,
    RegistryError,
    ServerInUseError,
    ServerNotFoundError,
    ServerRegistration,
    delete_server,
    register_server,
)
from mcprouter.discovery.sync import DiscoveryService, SyncReport, apply_listing

__all__ = [
    "DiscoveryService",
    "DuplicateServerError",
    "HealthPolicy",
    "HealthTracker",
    "InvalidRegistrationError",
    "RegistryError",
    "ServerInUseError",
    "ServerNotFoundError",
    "ServerRegistration",
    "SyncLoop",
    "SyncReport",
    "apply_listing",
    "delete_server",
    "next_status",
    "register_server",
]
