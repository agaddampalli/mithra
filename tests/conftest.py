"""
Shared pytest fixtures for both unit and integration test suites.

Unit fixtures provide in-memory or tmp-path backed instances of adapters.
Integration fixtures connect to the real Docker services and are skipped
automatically when the ENVIRONMENT variable is not set to 'test'.
"""

from __future__ import annotations

import json
from typing import Any

import pytest


class MockGraphClient:
    """In-memory Graph API client for unit tests.

    Stores records and files in plain dicts. Supports configurable failure
    injection via fail_on / fail_after for resilience tests.
    """

    def __init__(self) -> None:
        self._records: dict[str, dict] = {}   # path → JSON payload
        self._files: dict[str, bytes] = {}    # path → raw bytes
        self.call_log: list[tuple[str, str]] = []  # (method, path)
        self.fail_on: str | None = None        # method name to always fail, or "all"
        self.fail_after: int = 0              # fail after this many successful calls
        self._call_count: int = 0

    def _should_fail(self, method: str) -> bool:
        if self.fail_on is None:
            return False
        if self.fail_on not in (method, "all"):
            return False
        if self.fail_after > 0:
            self._call_count += 1
            return self._call_count > self.fail_after
        return True

    def get_record(self, path: str) -> dict | None:
        self.call_log.append(("get_record", path))
        if self._should_fail("get_record"):
            raise ConnectionError("Mock graph failure")
        return self._records.get(path)

    def put_record(self, path: str, data: dict) -> None:
        self.call_log.append(("put_record", path))
        if self._should_fail("put_record"):
            raise ConnectionError("Mock graph failure")
        self._records[path] = data

    def delete_record(self, path: str) -> None:
        self.call_log.append(("delete_record", path))
        if self._should_fail("delete_record"):
            raise ConnectionError("Mock graph failure")
        self._records.pop(path, None)

    def get_file(self, path: str) -> bytes:
        self.call_log.append(("get_file", path))
        if self._should_fail("get_file"):
            raise ConnectionError("Mock graph failure")
        if path not in self._files:
            raise FileNotFoundError(path)
        return self._files[path]

    def put_file(self, path: str, content: bytes, mime_type: str) -> None:
        self.call_log.append(("put_file", path))
        if self._should_fail("put_file"):
            raise ConnectionError("Mock graph failure")
        self._files[path] = content

    def delete_file(self, path: str) -> bool:
        self.call_log.append(("delete_file", path))
        if self._should_fail("delete_file"):
            raise ConnectionError("Mock graph failure")
        if path in self._files:
            del self._files[path]
            return True
        return False

    def list_records(self, domain: str, layer: str, user_id: str) -> list[dict]:
        self.call_log.append(("list_records", f"{domain}/{layer}/{user_id}"))
        if self._should_fail("list_records"):
            raise ConnectionError("Mock graph failure")
        # Match all records stored under this (domain, layer, user_id) folder
        prefix = f"/me/drive/root:/PLOS/{domain}/{layer}/{user_id}/"
        return [v for k, v in self._records.items() if k.startswith(prefix)]

    def check_connectivity(self) -> bool:
        self.call_log.append(("check_connectivity", ""))
        if self.fail_on in ("check_connectivity", "all"):
            return False
        return True

    @property
    def graph_call_methods(self) -> list[str]:
        return [m for m, _ in self.call_log]
