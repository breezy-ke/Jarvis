"""Encryption at rest for tokens and other secrets (Fernet: AES-128-CBC + HMAC).

`JARVIS_SECRET_KEY` holds one key, or several separated by commas for rotation.
The first key encrypts; all of them can decrypt.
"""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken, MultiFernet


class VaultError(RuntimeError):
    pass


class Vault:
    def __init__(self, keys: str) -> None:
        parts = [k.strip() for k in keys.split(",") if k.strip()]
        if not parts:
            raise VaultError("JARVIS_SECRET_KEY is empty. Run `make secrets` to generate one.")
        try:
            self._fernet = MultiFernet([Fernet(k.encode()) for k in parts])
        except (ValueError, TypeError) as exc:  # malformed key
            raise VaultError(
                "JARVIS_SECRET_KEY is not a valid Fernet key. Run `make secrets`."
            ) from exc

    @staticmethod
    def generate_key() -> str:
        return Fernet.generate_key().decode()

    def encrypt(self, data: str | bytes) -> bytes:
        raw = data.encode("utf-8") if isinstance(data, str) else data
        return self._fernet.encrypt(raw)

    def decrypt(self, token: bytes) -> bytes:
        try:
            return self._fernet.decrypt(token)
        except InvalidToken as exc:
            raise VaultError(
                "Could not decrypt stored data: wrong or rotated JARVIS_SECRET_KEY"
            ) from exc

    def decrypt_str(self, token: bytes) -> str:
        return self.decrypt(token).decode("utf-8")
