"""Integration tests: EncryptedStorageAdapter + LocalStorageAdapter + StorageRouter.

These tests verify the encryption boundary — that ciphertext is stored and plaintext
is returned — by reading the inner adapter directly alongside the encrypted adapter.
"""

from __future__ import annotations

import base64
import os

import pytest

from mithra.storage.adapter import StorageRecord
from mithra.storage.encryption import AESGCMCipher, EncryptedStorageAdapter
from mithra.storage.local import LocalStorageAdapter
from mithra.storage.router import StorageRouter


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def cipher():
    key = base64.b64encode(os.urandom(32)).decode()
    return AESGCMCipher(key)


@pytest.fixture
def inner(tmp_path):
    return LocalStorageAdapter(
        base_path=str(tmp_path / "files"),
        db_path=str(tmp_path / "db.db"),
    )


@pytest.fixture
def enc_all(inner, cipher):
    """EncryptedStorageAdapter with encrypted_fields=None (encrypt every key)."""
    return EncryptedStorageAdapter(inner, cipher, encrypted_fields=None)


@pytest.fixture
def enc_partial(inner, cipher):
    """EncryptedStorageAdapter with encrypted_fields=['amount'] only."""
    return EncryptedStorageAdapter(inner, cipher, encrypted_fields=["amount"])


def _rec(data: dict, key: str = "k1", domain: str = "health") -> StorageRecord:
    return StorageRecord(
        domain=domain,
        key=key,
        layer="facts",
        data=data,
        user_id="person_a",
        version=1,
        updated_at="",
    )


# ---------------------------------------------------------------------------
# Record encryption at rest
# ---------------------------------------------------------------------------


class TestRecordEncryptionAtRest:
    def test_raw_read_does_not_contain_plaintext(self, enc_all, inner):
        """Writing via encrypted adapter stores ciphertext — inner sees no plaintext."""
        enc_all.write(_rec({"name": "Alice", "diagnosis": "flu"}))
        raw = inner.read("health", "k1", "facts", "person_a")
        assert raw is not None
        raw_str = str(raw.data)
        assert "Alice" not in raw_str
        assert "flu" not in raw_str

    def test_encrypted_read_returns_plaintext(self, enc_all):
        """Reading via encrypted adapter restores original values."""
        enc_all.write(_rec({"name": "Alice", "diagnosis": "flu"}))
        rec = enc_all.read("health", "k1", "facts", "person_a")
        assert rec is not None
        assert rec.data["name"] == "Alice"
        assert rec.data["diagnosis"] == "flu"

    def test_two_writes_produce_different_ciphertext(self, enc_all, inner):
        """Nonce is unique per call — identical plaintext encrypts to different ciphertext."""
        enc_all.write(_rec({"value": 42}, key="k1"))
        enc_all.write(_rec({"value": 42}, key="k2"))
        raw1 = inner.read("health", "k1", "facts", "person_a")
        raw2 = inner.read("health", "k2", "facts", "person_a")
        assert raw1.data["value"] != raw2.data["value"]

    def test_read_missing_returns_none(self, enc_all):
        assert enc_all.read("health", "nonexistent", "facts", "person_a") is None

    def test_version_propagates_through_encryption(self, enc_all):
        """Version increments correctly despite the encryption wrapper."""
        enc_all.write(_rec({"v": 1}))
        rec = enc_all.write(_rec({"v": 2}))
        assert rec.version == 2

    def test_updated_at_is_set_after_write(self, enc_all):
        rec = enc_all.write(_rec({"v": 1}))
        assert rec.updated_at != ""


# ---------------------------------------------------------------------------
# Partial field encryption
# ---------------------------------------------------------------------------


class TestPartialFieldEncryption:
    def test_all_keys_encrypted_when_fields_is_none(self, enc_all, inner):
        """encrypted_fields=None → every key in data is ciphertext at rest."""
        enc_all.write(_rec({"amount": 100, "note": "salary", "tag": "income"}))
        raw = inner.read("health", "k1", "facts", "person_a")
        raw_str = str(raw.data)
        assert "100" not in raw_str
        assert "salary" not in raw_str
        assert "income" not in raw_str

    def test_only_listed_field_encrypted(self, enc_partial, inner):
        """encrypted_fields=['amount'] → only 'amount' is ciphertext; others are plaintext."""
        enc_partial.write(_rec({"amount": 500, "currency": "GBP", "note": "rent"}))
        raw = inner.read("health", "k1", "facts", "person_a")
        raw_str = str(raw.data)
        assert "GBP" in raw_str          # plaintext field readable in raw store
        assert "rent" in raw_str         # plaintext field readable in raw store
        assert "500" not in raw_str      # encrypted field must not appear as plaintext

    def test_unlisted_fields_readable_after_partial_encrypt(self, enc_partial):
        """Reading via encrypted adapter returns plaintext for both encrypted and plain fields."""
        enc_partial.write(_rec({"amount": 500, "currency": "GBP"}))
        rec = enc_partial.read("health", "k1", "facts", "person_a")
        assert rec.data["amount"] == 500
        assert rec.data["currency"] == "GBP"


# ---------------------------------------------------------------------------
# list() decrypts all records
# ---------------------------------------------------------------------------


class TestListDecryption:
    def test_list_returns_plaintext_for_all_records(self, enc_all):
        enc_all.write(_rec({"score": 1}, key="a"))
        enc_all.write(_rec({"score": 2}, key="b"))
        enc_all.write(_rec({"score": 3}, key="c"))
        records = enc_all.list("health", "facts", "person_a")
        scores = {r.key: r.data["score"] for r in records}
        assert scores == {"a": 1, "b": 2, "c": 3}

    def test_list_inner_contains_no_plaintext(self, enc_all, inner):
        enc_all.write(_rec({"secret": "private"}, key="a"))
        enc_all.write(_rec({"secret": "hidden"}, key="b"))
        raw_records = inner.list("health", "facts", "person_a")
        raw_str = " ".join(str(r.data) for r in raw_records)
        assert "private" not in raw_str
        assert "hidden" not in raw_str


# ---------------------------------------------------------------------------
# File encryption at rest
# ---------------------------------------------------------------------------


class TestFileEncryption:
    def test_raw_file_bytes_are_not_original(self, enc_all, inner):
        """Writing a file via encrypted adapter stores ciphertext bytes."""
        original = b"medical report content"
        path = enc_all.write_file("health", "report.pdf", original, "application/pdf", "person_a")
        raw_bytes = inner.read_file(path)
        assert raw_bytes != original

    def test_encrypted_read_file_returns_original(self, enc_all):
        """Reading a file via encrypted adapter returns the original bytes."""
        original = b"medical report content"
        path = enc_all.write_file("health", "report.pdf", original, "application/pdf", "person_a")
        assert enc_all.read_file(path) == original

    def test_file_encryption_handles_binary_content(self, enc_all):
        """Arbitrary binary (not valid UTF-8) round-trips correctly."""
        original = bytes(range(256))
        path = enc_all.write_file("health", "binary.bin", original, "application/octet-stream", "person_a")
        assert enc_all.read_file(path) == original

    def test_delete_file_delegates_to_inner(self, enc_all):
        path = enc_all.write_file("health", "del.txt", b"data", "text/plain", "person_a")
        assert enc_all.delete_file(path) is True
        assert enc_all.delete_file(path) is False


# ---------------------------------------------------------------------------
# delete() and health() pass-through
# ---------------------------------------------------------------------------


class TestPassThroughOperations:
    def test_delete_removes_record(self, enc_all):
        enc_all.write(_rec({"v": 1}))
        assert enc_all.delete("health", "k1", "person_a") is True
        assert enc_all.read("health", "k1", "facts", "person_a") is None

    def test_delete_nonexistent_returns_false(self, enc_all):
        assert enc_all.delete("health", "ghost", "person_a") is False

    def test_health_includes_encrypted_flag(self, enc_all):
        h = enc_all.health()
        assert h.get("encrypted") is True

    def test_health_reports_encrypted_fields_all(self, enc_all):
        h = enc_all.health()
        assert h.get("encrypted_fields") == "all"

    def test_health_reports_encrypted_fields_list(self, enc_partial):
        h = enc_partial.health()
        assert h.get("encrypted_fields") == ["amount"]

    def test_health_status_ok(self, enc_all):
        assert enc_all.health()["status"] == "ok"


# ---------------------------------------------------------------------------
# StorageRouter end-to-end via encryption layer
# ---------------------------------------------------------------------------


class TestStorageRouterViaEncryption:
    @pytest.fixture
    def router(self, tmp_path, cipher):
        local = LocalStorageAdapter(str(tmp_path / "files"), str(tmp_path / "local.db"))
        onedrive = LocalStorageAdapter(str(tmp_path / "od_files"), str(tmp_path / "onedrive.db"))
        enc_local = EncryptedStorageAdapter(local, cipher)
        enc_onedrive = EncryptedStorageAdapter(onedrive, cipher, encrypted_fields=["amount"])
        return StorageRouter(
            local_adapter=local,
            onedrive_adapter=onedrive,
            encrypted_local_adapter=enc_local,
            encrypted_onedrive_adapter=enc_onedrive,
        ), local, onedrive

    def test_health_adapter_is_encrypted_local(self, router):
        r, local, _ = router
        adapter = r.get_adapter("health")
        assert isinstance(adapter, EncryptedStorageAdapter)
        assert adapter._inner is local

    def test_money_adapter_is_encrypted_onedrive(self, router):
        r, _, onedrive = router
        adapter = r.get_adapter("money")
        assert isinstance(adapter, EncryptedStorageAdapter)
        assert adapter._inner is onedrive

    def test_food_adapter_is_plain_onedrive(self, router):
        r, _, onedrive = router
        adapter = r.get_adapter("food")
        assert adapter is onedrive
        assert not isinstance(adapter, EncryptedStorageAdapter)

    def test_health_write_read_round_trip(self, router):
        r, local, _ = router
        adapter = r.get_adapter("health")
        adapter.write(_rec({"bp": "120/80"}, domain="health"))
        # Verify ciphertext in inner store
        raw = local.read("health", "k1", "facts", "person_a")
        assert "120/80" not in str(raw.data)
        # Verify plaintext via router
        rec = adapter.read("health", "k1", "facts", "person_a")
        assert rec.data["bp"] == "120/80"

    def test_health_domain_never_returns_unencrypted_adapter(self, router):
        r, _, _ = router
        adapter = r.get_adapter("health")
        # Must not be the bare local or onedrive adapter
        assert not (type(adapter) is LocalStorageAdapter)
