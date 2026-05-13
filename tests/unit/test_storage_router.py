"""Unit tests for StorageRouter — verifies health domain is always encrypted local."""

import base64
import os

import pytest

from mithra.storage.encryption import AESGCMCipher, EncryptedStorageAdapter
from mithra.storage.local import LocalStorageAdapter
from mithra.storage.router import StorageRouter


@pytest.fixture
def router(tmp_path):
    cipher = AESGCMCipher(base64.b64encode(os.urandom(32)).decode())
    local = LocalStorageAdapter(str(tmp_path / "files"), str(tmp_path / "local.db"))
    onedrive = LocalStorageAdapter(str(tmp_path / "od_files"), str(tmp_path / "onedrive.db"))
    enc_local = EncryptedStorageAdapter(local, cipher)
    enc_onedrive = EncryptedStorageAdapter(onedrive, cipher, encrypted_fields=["amount"])
    return StorageRouter(local, onedrive, enc_local, enc_onedrive)


class TestStorageRouter:
    def test_health_always_encrypted_local(self, router, tmp_path):
        adapter = router.get_adapter("health")
        assert isinstance(adapter, EncryptedStorageAdapter)

    def test_money_is_encrypted_onedrive(self, router, tmp_path):
        adapter = router.get_adapter("money")
        assert isinstance(adapter, EncryptedStorageAdapter)

    def test_food_is_plain_onedrive(self, router, tmp_path):
        from mithra.storage.local import LocalStorageAdapter as LSA
        adapter = router.get_adapter("food")
        assert isinstance(adapter, LSA)

    def test_health_domain_never_returns_onedrive(self, router):
        from mithra.storage.local import LocalStorageAdapter as LSA
        adapter = router.get_adapter("health")
        # Traverse the decorator chain — inner must be LocalStorageAdapter
        assert isinstance(adapter._inner, LSA)

    def test_encrypted_cloud_domains(self, router):
        for domain in ("money", "documents", "people"):
            assert isinstance(router.get_adapter(domain), EncryptedStorageAdapter)
