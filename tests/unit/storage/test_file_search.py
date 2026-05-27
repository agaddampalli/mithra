"""Unit tests for FileSearchService — metadata tier and health domain filtering."""

import tempfile
import os
import pytest

from mithra.storage.local import LocalStorageAdapter
from mithra.storage.local_file import LocalFileAdapter
from mithra.storage.file_router import FileStorageRouter
from mithra.storage.file_search import FileSearchService


@pytest.fixture()
def setup():
    with tempfile.TemporaryDirectory() as base, tempfile.TemporaryDirectory() as db_dir:
        db_path = os.path.join(db_dir, "test.db")
        _ = LocalStorageAdapter(base_path=base, db_path=db_path)
        local_file = LocalFileAdapter(base_path=base, db_path=db_path)
        router = FileStorageRouter(local_adapter=local_file)
        yield local_file, router, db_path


class FakeChroma:
    """Minimal ChromaDB stub for unit tests."""
    def get_collection(self, name):
        raise Exception("no collection")

    def get_or_create_collection(self, name):
        return self


@pytest.fixture()
def svc(setup):
    _, router, _ = setup
    chroma = FakeChroma()
    return FileSearchService(file_router=router, chroma_client=chroma, household_id="hh_1")


@pytest.fixture()
def populated(setup):
    local_file, router, _ = setup
    local_file.save_file("hh_1", "usr_1", "documents", "report.pdf", b"pdf", mime_type="application/pdf")
    local_file.save_file("hh_1", "usr_1", "documents", "notes.txt", b"txt", mime_type="text/plain")
    local_file.save_file("hh_1", "usr_2", "documents", "invoice.pdf", b"inv", mime_type="application/pdf")
    local_file.save_file("hh_1", "usr_1", "health", "bloodwork.pdf", b"bw", mime_type="application/pdf")
    chroma = FakeChroma()
    return FileSearchService(file_router=router, chroma_client=chroma, household_id="hh_1")


def test_search_metadata_by_domain_and_user(populated):
    results = populated.search_metadata("documents", "usr_1")
    names = {r.record.filename for r in results}
    assert "report.pdf" in names
    assert "notes.txt" in names
    assert "invoice.pdf" not in names  # belongs to usr_2


def test_search_metadata_filename_fragment(populated):
    results = populated.search_metadata("documents", "usr_1", filename_fragment="report")
    assert len(results) == 1
    assert results[0].record.filename == "report.pdf"


def test_search_metadata_mime_type_prefix(populated):
    results = populated.search_metadata("documents", "usr_1", mime_type_prefix="application/")
    assert all(r.record.mime_type.startswith("application/") for r in results)


def test_search_metadata_returns_tier1(populated):
    results = populated.search_metadata("documents", "usr_1")
    assert all(r.tier == 1 for r in results)


def test_search_semantic_no_collection_returns_empty(populated):
    results = populated.search_semantic("blood pressure", "health", "usr_1")
    assert results == []


def test_search_metadata_empty_domain(svc):
    results = svc.search_metadata("documents", "usr_1")
    assert results == []


def test_get_file_content(setup):
    local_file, router, _ = setup
    rec = local_file.save_file("hh_1", "usr_1", "documents", "raw.txt", b"raw content")
    chroma = FakeChroma()
    svc = FileSearchService(file_router=router, chroma_client=chroma, household_id="hh_1")
    content = svc.get_file_content(rec.id, "documents", "hh_1")
    assert content == b"raw content"
