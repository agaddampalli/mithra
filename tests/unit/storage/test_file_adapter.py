"""Unit tests for FileStorageAdapter abstract base and FileRecord."""

from mithra.storage.file_adapter import FileRecord, FileStorageAdapter, generate_file_id


def test_generate_file_id_prefix():
    fid = generate_file_id()
    assert fid.startswith("file_")


def test_generate_file_id_unique():
    ids = {generate_file_id() for _ in range(100)}
    assert len(ids) == 100


def test_file_record_defaults():
    r = FileRecord(
        id="file_abc",
        household_id="hh_1",
        user_id="usr_1",
        domain="documents",
        filename="test.pdf",
        storage="local",
        path="/tmp/test.pdf",
        size_bytes=1024,
        mime_type="application/pdf",
        uploaded_at="2026-01-01T00:00:00+00:00",
    )
    assert r.metadata == {}
    assert r.chunk_ids == []


def test_file_storage_adapter_is_abstract():
    import inspect
    assert inspect.isabstract(FileStorageAdapter)


def test_file_storage_adapter_abstract_methods():
    import inspect
    abstract = {
        name
        for name, val in inspect.getmembers(FileStorageAdapter)
        if getattr(val, "__isabstractmethod__", False)
    }
    expected = {"save_file", "get_file", "delete_file", "list_files", "get_record", "update_chunk_ids", "health"}
    assert expected <= abstract
