from __future__ import annotations

import base64
import os
from pathlib import Path
import secrets
from threading import Lock

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

SECRET_MASK = '********'
_ENC_PREFIX = 'enc:v1:'
_KEY_LOCK = Lock()
_CACHED_KEY: bytes | None = None


def mask_secret(value: str | None) -> str:
    return SECRET_MASK if value else ''


def is_masked_secret(value: str | None) -> bool:
    return bool(value) and value == SECRET_MASK


def is_encrypted_secret(value: str | None) -> bool:
    return bool(value) and value.startswith(_ENC_PREFIX)


def _secrets_key_path() -> Path:
    raw = os.getenv('OPTIMIZARR_SECRETS_KEY_PATH', '/config/optimizarr.secrets.key').strip()
    return Path(raw)


def _decode_key(raw: str) -> bytes:
    candidate = raw.strip()
    if candidate.startswith('base64:'):
        candidate = candidate.split(':', 1)[1]
    decoded = base64.urlsafe_b64decode(candidate + ('=' * ((4 - len(candidate) % 4) % 4)))
    if len(decoded) != 32:
        raise ValueError('Secret key must decode to 32 bytes')
    return decoded


def _load_or_create_file_key(path: Path) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        content = path.read_text(encoding='utf-8').strip()
        if not content:
            raise ValueError(f'Secrets key file is empty: {path}')
        return _decode_key(content)

    key = secrets.token_bytes(32)
    encoded = base64.urlsafe_b64encode(key).decode('ascii').rstrip('=')
    path.write_text(encoded, encoding='utf-8')
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return key


def _get_key() -> bytes:
    global _CACHED_KEY
    if _CACHED_KEY is not None:
        return _CACHED_KEY

    with _KEY_LOCK:
        if _CACHED_KEY is not None:
            return _CACHED_KEY
        env_key = (os.getenv('OPTIMIZARR_SECRETS_KEY') or '').strip()
        if env_key:
            _CACHED_KEY = _decode_key(env_key)
            return _CACHED_KEY
        _CACHED_KEY = _load_or_create_file_key(_secrets_key_path())
        return _CACHED_KEY


def encrypt_secret(plaintext: str | None) -> str:
    if not plaintext:
        return ''
    if is_encrypted_secret(plaintext):
        return plaintext

    aes = AESGCM(_get_key())
    nonce = secrets.token_bytes(12)
    ciphertext = aes.encrypt(nonce, plaintext.encode('utf-8'), None)
    payload = base64.urlsafe_b64encode(nonce + ciphertext).decode('ascii').rstrip('=')
    return f'{_ENC_PREFIX}{payload}'


def decrypt_secret(value: str | None) -> str:
    if not value:
        return ''
    if not is_encrypted_secret(value):
        return value

    encoded = value[len(_ENC_PREFIX):]
    raw = base64.urlsafe_b64decode(encoded + ('=' * ((4 - len(encoded) % 4) % 4)))
    nonce, ciphertext = raw[:12], raw[12:]
    aes = AESGCM(_get_key())
    plaintext = aes.decrypt(nonce, ciphertext, None)
    return plaintext.decode('utf-8')
