"""Unit tests for LocalFileAdapter."""

import tempfile
import os
import pytest

from mithra.storage.local import LocalStorageAdapter
from mithra.storage.local_file import LocalFileAdapter


@pytest.fixture()
def tmp_dirs():
    with tempfile.TemporaryDirectory() as base, tempfile.TemporaryDirectory() as db_dir:
        db_path = os.path.join(db_dir, "test.db")
        # LocalStorageAdapter creates the files table
        _ = LocalStorageAdapter(base_path=base, db_path=db_path)
        yield base, db_path


@pytest.fixture()
def adapter(tmp_dirs):
    base, db_path = tmp_dirs
    return LocalFileAdapter(base_path=base, db_path=db_path)


def test_save_file_returns_record(adapter):
    rec = adapter.save_file(
        household_id="hh_1",
        user_id="usr_1",
        domain="documents",
        filename="test.pdf",
        content=b"hello pdf",
        mime_type="application/pdf",
    )
    assert rec.id.startswith("file_")
    assert rec.storage == "local"
    assert rec.size_bytes == 9
    assert rec.filename == "test.pdf"


def test_save_and_get_file(adapter):
    rec = adapter.save_file("hh_1", "usr_1", "documents", "a.txt", b"content here")
    data = adapter.get_file(rec.id, "hh_1")
    assert data == b"content here"


def test_get_record_returns_metadata(adapter):
    rec = adapter.save_file("hh_1", "usr_1", "documents", "b.txt", b"xyz")
    fetched = adapter.get_record(rec.id, "hh_1")
    assert fetched is not None
    assert fetched.id == rec.id
    assert fetched.filename == "b.txt"


def test_get_record_missing_returns_none(adapter):
    assert adapter.get_record("file_nonexistent", "hh_1") is None


def test_delete_file(adapter):
    rec = adapter.save_file("hh_1", "usr_1", "documents", "del.txt", b"bye")
    assert adapter.delete_file(rec.id, "hh_1") is True
    assert adapter.get_record(rec.id, "hh_1") is None


def test_delete_missing_returns_false(adapter):
    assert adapter.delete_file("file_nope", "hh_1") is False


def test_list_files(adapter):
    adapter.save_file("hh_1", "usr_1", "documents", "c1.txt", b"a")
    adapter.save_file("hh_1", "usr_1", "documents", "c2.txt", b"b")
    adapter.save_file("hh_1", "usr_2", "documents", "c3.txt", b"c")
    all_recs = adapter.list_files("hh_1", "documents")
    assert len(all_recs) == 3
    user_recs = adapter.list_files("hh_1", "documents", user_id="usr_1")
    assert len(user_recs) == 2


def test_update_chunk_ids(adapter):
    rec = adapter.save_file("hh_1", "usr_1", "documents", "idx.txt", b"text")
    adapter.update_chunk_ids(rec.id, "hh_1", ["chunk_0", "chunk_1"])
    fetched = adapter.get_record(rec.id, "hh_1")
    assert fetched.chunk_ids == ["chunk_0", "chunk_1"]


def test_health_ok(adapter):
    h = adapter.health()
    assert h["status"] == "ok"
    assert h["backend"] == "local"


def test_save_file_with_metadata(adapter):
    meta = {"source": "upload", "page_count": 3}
    rec = adapter.save_file("hh_1", "usr_1", "documents", "meta.pdf", b"data", metadata=meta)
    fetched = adapter.get_record(rec.id, "hh_1")
    assert fetched.metadata == meta


def test_files_isolated_by_household(adapter):
    adapter.save_file("hh_A", "usr_1", "documents", "x.txt", b"a")
    adapter.save_file("hh_B", "usr_1", "documents", "y.txt", b"b")
    assert len(adapter.list_files("hh_A", "documents")) == 1
    assert len(adapter.list_files("hh_B", "documents")) == 1


def test_get_file_wrong_household_raises(adapter):
    rec = adapter.save_file("hh_A", "usr_1", "documents", "z.txt", b"data")
    with pytest.raises(FileNotFoundError):
        adapter.get_file(rec.id, "hh_B")
