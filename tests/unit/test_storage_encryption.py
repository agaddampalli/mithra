"""Unit tests for AESGCMCipher and EncryptedStorageAdapter."""

import base64
import os
import tempfile

import pytest

from mithra.storage.adapter import StorageRecord
from mithra.storage.encryption import AESGCMCipher, EncryptedStorageAdapter
from mithra.storage.local import LocalStorageAdapter


@pytest.fixture
def cipher():
    key = base64.b64encode(os.urandom(32)).decode()
    return AESGCMCipher(key)


@pytest.fixture
def local(tmp_path):
    return LocalStorageAdapter(str(tmp_path / "files"), str(tmp_path / "db.db"))


@pytest.fixture
def encrypted(local, cipher):
    return EncryptedStorageAdapter(local, cipher)


@pytest.fixture
def encrypted_fields(local, cipher):
    return EncryptedStorageAdapter(local, cipher, encrypted_fields=["secret"])


def _rec(data=None, key="k1"):
    return StorageRecord(
        domain="health",
        key=key,
        layer="facts",
        data=data or {"value": 99},
        user_id="person_a",
        version=1,
        updated_at="",
    )


class TestAESGCMCipher:
    def test_encrypt_decrypt_bytes(self, cipher):
        plain = b"sensitive data"
        ct = cipher.encrypt(plain)
        assert cipher.decrypt(ct) == plain

    def test_encrypt_produces_different_ciphertext(self, cipher):
        plain = b"same input"
        assert cipher.encrypt(plain) != cipher.encrypt(plain)

    def test_encrypt_field_round_trip(self, cipher):
        assert cipher.decrypt_field(cipher.encrypt_field("hello")) == "hello"
        assert cipher.decrypt_field(cipher.encrypt_field(42)) == 42
        assert cipher.decrypt_field(cipher.encrypt_field({"a": 1})) == {"a": 1}

    def test_invalid_key_length(self):
        with pytest.raises(ValueError):
            AESGCMCipher(base64.b64encode(b"tooshort").decode())


class TestEncryptedStorageAdapter:
    def test_write_read_all_fields(self, encrypted):
        encrypted.write(_rec(data={"value": 123}))
        rec = encrypted.read("health", "k1", "facts", "person_a")
        assert rec.data["value"] == 123

    def test_data_is_encrypted_at_rest(self, encrypted, local):
        encrypted.write(_rec(data={"secret": "my_secret"}))
        raw = local.read("health", "k1", "facts", "person_a")
        # The raw record must not contain the plaintext
        assert "my_secret" not in str(raw.data)

    def test_field_level_encryption(self, encrypted_fields, local):
        encrypted_fields.write(_rec(data={"secret": "password", "public": "visible"}))
        raw = local.read("health", "k1", "facts", "person_a")
        assert "visible" in str(raw.data)  # unencrypted field readable
        assert "password" not in str(raw.data)  # encrypted field not readable

    def test_list_decrypts_all(self, encrypted):
        encrypted.write(_rec(key="a", data={"v": 1}))
        encrypted.write(_rec(key="b", data={"v": 2}))
        recs = encrypted.list("health", "facts", "person_a")
        values = {r.key: r.data["v"] for r in recs}
        assert values == {"a": 1, "b": 2}

    def test_delete(self, encrypted):
        encrypted.write(_rec())
        assert encrypted.delete("health", "k1", "person_a") is True

    def test_file_encrypt_decrypt_round_trip(self, encrypted, tmp_path):
        content = b"binary file content"
        path = encrypted.write_file("health", "doc.pdf", content, "application/pdf", "person_a")
        assert encrypted.read_file(path) == content
