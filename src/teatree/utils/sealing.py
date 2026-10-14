"""Authenticated sealing for instance credentials."""

import base64
import binascii
import os
from dataclasses import dataclass, field

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from teatree.utils.secrets import SecretStoreError

MASTER_KEY_BYTES = 32


class InvalidMasterKeyError(SecretStoreError, ValueError):
    def __init__(self, source: str, reason: str) -> None:
        super().__init__(f"master key at {source} {reason}")


class WrongMasterKeyError(SecretStoreError, ValueError):
    def __init__(self, stored_id: str, active_id: str) -> None:
        super().__init__(f"sealed key_id {stored_id} differs from active key_id {active_id}")


class SealedValueTamperedError(SecretStoreError, ValueError):
    def __init__(self) -> None:
        super().__init__("sealed value failed authentication")


@dataclass(frozen=True, slots=True)
class SealedValue:
    key_id: str
    nonce: bytes
    ciphertext: bytes


def aad(scope: str, name: str) -> bytes:
    if not scope or not name or "\0" in scope or "\0" in name:
        message = "scope and name must be non-empty and contain no NUL"
        raise ValueError(message)
    return b"teatree-sealed/v1\0" + scope.encode() + b"\0" + name.encode()


def _derive(value: bytes, info: bytes, length: int) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=None, info=info).derive(value)


@dataclass(frozen=True, slots=True)
class MasterKey:
    _value: bytes = field(repr=False)
    key_id: str = field(init=False)

    def __post_init__(self) -> None:
        if len(self._value) != MASTER_KEY_BYTES:
            source = "direct value"
            reason = "must decode to 32 bytes"
            raise InvalidMasterKeyError(source, reason)
        object.__setattr__(self, "key_id", _derive(self._value, b"teatree/key-id/v1", 8).hex())

    @classmethod
    def from_text(cls, text: str, *, source: str) -> "MasterKey":
        value = text.removesuffix("\n")
        if "-" in value or "_" in value:
            raise InvalidMasterKeyError(source, "must use standard base64")
        try:
            decoded = base64.b64decode(value, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise InvalidMasterKeyError(source, "is not standard base64") from exc
        if len(decoded) != MASTER_KEY_BYTES:
            raise InvalidMasterKeyError(source, "must decode to 32 bytes")
        return cls(decoded)

    def seal(self, plaintext: str, *, aad: bytes) -> SealedValue:
        nonce = os.urandom(12)
        ciphertext = AESGCM(_derive(self._value, b"teatree/seal/v1", 32)).encrypt(nonce, plaintext.encode(), aad)
        return SealedValue(self.key_id, nonce, ciphertext)

    def open(self, value: SealedValue, *, aad: bytes) -> str:
        if value.key_id != self.key_id:
            raise WrongMasterKeyError(value.key_id, self.key_id)
        try:
            plaintext = AESGCM(_derive(self._value, b"teatree/seal/v1", 32)).decrypt(value.nonce, value.ciphertext, aad)
        except InvalidTag as exc:
            raise SealedValueTamperedError from exc
        return plaintext.decode()
