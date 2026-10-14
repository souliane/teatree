import base64
import subprocess
import sys
from unittest.mock import patch

import pytest

from teatree.utils.sealing import InvalidMasterKeyError, MasterKey, SealedValueTamperedError, WrongMasterKeyError, aad
from teatree.utils.secrets import SecretStoreError

KEY_BYTES = bytes((index * 23 + 41) % 256 for index in range(32))
KEY_TEXT = base64.b64encode(KEY_BYTES).decode()


def test_fixture_is_shuffled_standard_base64() -> None:
    assert "+" in KEY_TEXT
    assert "/" in KEY_TEXT
    assert bytes(sorted(KEY_BYTES)) != KEY_BYTES
    assert KEY_BYTES[::-1] != KEY_BYTES


def test_seal_opens_and_binds_name() -> None:
    key = MasterKey.from_text(KEY_TEXT, source="test")
    row = key.seal("canary", aad=aad("instance", "alpha/token"))
    assert key.open(row, aad=aad("instance", "alpha/token")) == "canary"
    assert KEY_TEXT not in repr(key)
    with pytest.raises(SealedValueTamperedError):
        key.open(row, aad=aad("instance", "beta/token"))


def test_fixed_vector_and_unique_nonces() -> None:
    key = MasterKey.from_text(KEY_TEXT, source="test")
    assert key.key_id == "12122fbb02ee264d"
    with patch("teatree.utils.sealing.os.urandom", return_value=bytes(range(12))):
        vector = key.seal("pässwörd", aad=aad("instance", "alpha/token"))
    assert vector.nonce == bytes(range(12))
    assert vector.ciphertext.hex() == "eac389ca28f62fdf9aa9bbcc9c319fdf9700b8a5bd66b62b5476"
    assert key.open(vector, aad=aad("instance", "alpha/token")) == "pässwörd"
    assert key.seal("same", aad=b"name").nonce != key.seal("same", aad=b"name").nonce


def test_crypto_errors_are_store_errors() -> None:
    assert issubclass(InvalidMasterKeyError, SecretStoreError)
    assert issubclass(WrongMasterKeyError, SecretStoreError)
    assert issubclass(SealedValueTamperedError, SecretStoreError)


def test_facade_import_does_not_load_crypto() -> None:
    result = subprocess.run(
        [sys.executable, "-c", "import sys; import teatree.utils.secrets; print('cryptography' in sys.modules)"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "False"


@pytest.mark.parametrize(
    "wrong", [KEY_BYTES[::-1], bytes(b ^ 1 for b in KEY_BYTES), KEY_BYTES[:-1] + bytes([KEY_BYTES[-1] ^ 1])]
)
def test_wrong_key_names_both_ids(wrong: bytes) -> None:
    key = MasterKey.from_text(KEY_TEXT, source="test")
    other = MasterKey.from_text(base64.b64encode(wrong).decode(), source="other")
    row = key.seal("canary", aad=aad("instance", "alpha"))
    with pytest.raises(WrongMasterKeyError) as exc:
        other.open(row, aad=aad("instance", "alpha"))
    assert key.key_id in str(exc.value)
    assert other.key_id in str(exc.value)


@pytest.mark.parametrize(
    "bad",
    [
        base64.urlsafe_b64encode(KEY_BYTES).decode(),
        base64.b64encode(KEY_BYTES[:31]).decode(),
        base64.b64encode(KEY_BYTES + b"x").decode(),
        "not base64",
    ],
)
def test_invalid_key_names_source_without_leaking_key(bad: str) -> None:
    with pytest.raises(ValueError, match="source:test") as exc:
        MasterKey.from_text(bad, source="source:test")
    assert bad not in str(exc.value)


def test_same_bytes_with_trailing_newline_reopen() -> None:
    key = MasterKey.from_text(KEY_TEXT, source="one")
    same = MasterKey.from_text(KEY_TEXT + "\n", source="two")
    row = key.seal("canary", aad=aad("instance", "alpha"))
    assert same.key_id == key.key_id
    assert same.open(row, aad=aad("instance", "alpha")) == "canary"
