"""Unit tests for FileStorageRouter — especially the health domain hard rule."""

import tempfile
import os
import pytest

from mithra.storage.local import LocalStorageAdapter
from mithra.storage.local_file import LocalFileAdapter
from mithra.storage.file_router import FileStorageRouter


@pytest.fixture()
def local_adapter():
    with tempfile.TemporaryDirectory() as base, tempfile.TemporaryDirectory() as db_dir:
        db_path = os.path.join(db_dir, "test.db")
        _ = LocalStorageAdapter(base_path=base, db_path=db_path)
        yield LocalFileAdapter(base_path=base, db_path=db_path)


@pytest.fixture()
def router_local_only(local_adapter):
    return FileStorageRouter(local_adapter=local_adapter)


@pytest.fixture()
def router_with_onedrive(local_adapter):
    class FakeOneDrive:
        def health(self):
            return {"status": "ok", "backend": "onedrive"}
        def list_files(self, *a, **kw):
            return []
        def get_record(self, *a, **kw):
            return None
        def save_file(self, *a, **kw):
            return None
        def get_file(self, *a, **kw):
            return b""
        def delete_file(self, *a, **kw):
            return False
        def update_chunk_ids(self, *a, **kw):
            pass

    fake = FakeOneDrive()
    return FileStorageRouter(local_adapter=local_adapter, onedrive_adapter=fake), fake


def test_health_domain_always_routes_to_local(router_with_onedrive, local_adapter):
    router, fake = router_with_onedrive
    adapter = router.get_adapter("health")
    assert adapter is local_adapter


def test_non_health_routes_to_onedrive_when_configured(router_with_onedrive, local_adapter):
    router, fake = router_with_onedrive
    adapter = router.get_adapter("documents")
    assert adapter is fake


def test_non_health_falls_back_to_local_when_no_onedrive(router_local_only, local_adapter):
    adapter = router_local_only.get_adapter("documents")
    assert adapter is local_adapter


def test_all_non_health_domains_route_to_onedrive(router_with_onedrive):
    router, fake = router_with_onedrive
    for domain in ["documents", "money", "people", "food", "home", "goals"]:
        assert router.get_adapter(domain) is fake


def test_health_isolates_even_with_onedrive(router_with_onedrive, local_adapter):
    router, _ = router_with_onedrive
    # health must never touch OneDrive regardless of what is configured
    assert router.get_adapter("health") is local_adapter


def test_health_ok(router_local_only):
    h = router_local_only.health()
    assert h["status"] in ("ok", "degraded")
    assert "local" in h


def test_health_no_onedrive_not_configured_status(router_local_only):
    h = router_local_only.health()
    assert h["onedrive"]["status"] == "not_configured"
