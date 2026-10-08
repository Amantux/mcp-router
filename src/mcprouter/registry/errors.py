"""Typed registry/dedup errors. Each carries a CURATED, client-safe message —
API boundaries return `.message`, never `str()` of an arbitrary exception."""

from __future__ import annotations


class RegistryError(Exception):
    status_code = 400

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class InvalidArgument(RegistryError):
    status_code = 400


class ToolNotFound(RegistryError):
    status_code = 404

    def __init__(self) -> None:
        super().__init__("Tool not found.")


class SuggestionNotFound(RegistryError):
    status_code = 404

    def __init__(self) -> None:
        super().__init__("Duplicate suggestion not found.")


class InvalidTransition(RegistryError):
    status_code = 409
