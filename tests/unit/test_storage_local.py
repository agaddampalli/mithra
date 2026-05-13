"""Unit tests for LocalStorageAdapter."""

import os
import tempfile

import pytest

from mithra.storage.adapter import StorageRecord
from mithra.storage.local import LocalStorageAdapter


@pytest.fixture
def adapter(tmp_path):
    return LocalStorageAdapter(
        base_path=str(tmp_path / "files"),
        db_path=str(tmp_path / "db" / "test.db"),
    )


def _rec(domain="health", key="fact1", layer="facts", user_id="person_a", data=None):
    return StorageRecord(
        domain=domain,
        key=key,
        layer=layer,
        data=data or {"value": 42},
        user_id=user_id,
        version=1,
        updated_at="",
    )


class TestLocalStorageAdapter:
    def test_write_and_read(self, adapter):
        rec = adapter.write(_rec())
        assert rec.version == 1
        assert rec.updated_at != ""

        fetched = adapter.read("health", "fact1", "facts", "person_a")
        assert fetched is not None
        assert fetched.data["value"] == 42

    def test_read_missing_returns_none(self, adapter):
        assert adapter.read("health", "nope", "facts", "person_a") is None

    def test_write_increments_version(self, adapter):
        adapter.write(_rec())
        rec2 = adapter.write(_rec())
        assert rec2.version == 2

    def test_list(self, adapter):
        adapter.write(_rec(key="a"))
        adapter.write(_rec(key="b"))
        records = adapter.list("health", "facts", "person_a")
        assert len(records) == 2

    def test_delete(self, adapter):
        adapter.write(_rec())
        deleted = adapter.delete("health", "fact1", "person_a")
        assert deleted is True
        assert adapter.read("health", "fact1", "facts", "person_a") is None

    def test_delete_nonexistent_returns_false(self, adapter):
        assert adapter.delete("health", "ghost", "person_a") is False

    def test_write_file_and_read_file(self, adapter):
        content = b"hello world"
        path = adapter.write_file("health", "test.txt", content, "text/plain", "person_a")
        assert adapter.read_file(path) == content

    def test_delete_file(self, adapter):
        path = adapter.write_file("health", "del.txt", b"data", "text/plain", "person_a")
        assert adapter.delete_file(path) is True
        assert adapter.delete_file(path) is False

    def test_health(self, adapter):
        h = adapter.health()
        assert h["status"] == "ok"
        assert "record_count" in h
