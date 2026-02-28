from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
from hashlib import pbkdf2_hmac, sha256
import hmac
import secrets
from urllib.parse import quote

from sqlalchemy.orm import Session

from app.models.auth import AdminUser, AuthSession

SESSION_COOKIE_NAME = 'optimizarr_session'
SESSION_MAX_AGE_SECONDS = 60 * 60 * 24 * 7  # 7 days
PBKDF2_ITERATIONS = 310_000
TOTP_STEP_SECONDS = 30
TOTP_DIGITS = 6
TOTP_ISSUER = 'Optimizarr'


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _token_hash(token: str) -> str:
    return sha256(token.encode('utf-8')).hexdigest()


def _b32_secret(raw: bytes) -> str:
    return base64.b32encode(raw).decode('ascii').rstrip('=')


def generate_totp_secret() -> str:
    return _b32_secret(secrets.token_bytes(20))


def totp_provisioning_uri(secret: str, username: str) -> str:
    account_name = quote(username, safe='')
    issuer = quote(TOTP_ISSUER, safe='')
    return f'otpauth://totp/{issuer}:{account_name}?secret={secret}&issuer={issuer}&algorithm=SHA1&digits=6&period=30'


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = pbkdf2_hmac('sha256', password.encode('utf-8'), salt, PBKDF2_ITERATIONS)
    salt_b64 = base64.b64encode(salt).decode('ascii')
    digest_b64 = base64.b64encode(digest).decode('ascii')
    return f'pbkdf2_sha256${PBKDF2_ITERATIONS}${salt_b64}${digest_b64}'


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        scheme, rounds, salt_b64, digest_b64 = stored_hash.split('$', 3)
        if scheme != 'pbkdf2_sha256':
            return False
        iterations = int(rounds)
        salt = base64.b64decode(salt_b64.encode('ascii'))
        expected = base64.b64decode(digest_b64.encode('ascii'))
    except Exception:
        return False

    actual = pbkdf2_hmac('sha256', password.encode('utf-8'), salt, iterations)
    return hmac.compare_digest(actual, expected)


def _normalize_b32(secret: str) -> bytes:
    compact = ''.join(secret.strip().upper().split())
    padding = '=' * ((8 - len(compact) % 8) % 8)
    return base64.b32decode(compact + padding, casefold=True)


def _totp_code(secret: str, for_time: int) -> str:
    key = _normalize_b32(secret)
    timestep = int(for_time // TOTP_STEP_SECONDS)
    counter = timestep.to_bytes(8, byteorder='big')
    digest = hmac.new(key, counter, 'sha1').digest()
    offset = digest[-1] & 0x0F
    binary = int.from_bytes(digest[offset:offset + 4], byteorder='big') & 0x7FFFFFFF
    code = binary % (10 ** TOTP_DIGITS)
    return f'{code:0{TOTP_DIGITS}d}'


def verify_totp_code(secret: str, code: str, *, at_time: int | None = None, window: int = 1) -> bool:
    if not secret:
        return False
    normalized_code = ''.join(ch for ch in code if ch.isdigit())
    if len(normalized_code) != TOTP_DIGITS:
        return False

    current_time = int(at_time if at_time is not None else datetime.now(tz=timezone.utc).timestamp())
    for step in range(-window, window + 1):
        candidate_time = current_time + (step * TOTP_STEP_SECONDS)
        if hmac.compare_digest(_totp_code(secret, candidate_time), normalized_code):
            return True
    return False


def current_totp_code(secret: str, *, at_time: int | None = None) -> str:
    current_time = int(at_time if at_time is not None else datetime.now(tz=timezone.utc).timestamp())
    return _totp_code(secret, current_time)


def normalize_username(username: str) -> str:
    return username.strip()


def validate_new_credentials(username: str, password: str) -> None:
    if len(username.strip()) < 3:
        raise ValueError('Username must be at least 3 characters long')
    if len(password) < 12:
        raise ValueError('Password must be at least 12 characters long')


def has_admin_user(db: Session) -> bool:
    return db.query(AdminUser.id).first() is not None


def get_user_by_username(db: Session, username: str) -> AdminUser | None:
    return db.query(AdminUser).filter(AdminUser.username == normalize_username(username)).first()


def create_admin_user(
    db: Session,
    *,
    username: str,
    password: str,
    two_factor_enabled: bool = False,
    totp_secret: str | None = None,
) -> AdminUser:
    normalized_username = normalize_username(username)
    validate_new_credentials(normalized_username, password)

    if db.query(AdminUser.id).first() is not None:
        raise ValueError('Admin user already configured')
    if two_factor_enabled and not totp_secret:
        raise ValueError('A TOTP secret is required when two-factor authentication is enabled')

    user = AdminUser(
        username=normalized_username,
        password_hash=hash_password(password),
        two_factor_enabled=bool(two_factor_enabled),
        totp_secret=totp_secret if two_factor_enabled else None,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def create_session(db: Session, user: AdminUser) -> tuple[str, AuthSession]:
    raw_token = secrets.token_urlsafe(48)
    expires_at = _utc_now() + timedelta(seconds=SESSION_MAX_AGE_SECONDS)
    session = AuthSession(
        user_id=user.id,
        token_hash=_token_hash(raw_token),
        expires_at=expires_at,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return raw_token, session


def purge_expired_sessions(db: Session) -> None:
    now = _utc_now()
    db.query(AuthSession).filter(AuthSession.expires_at <= now).delete(synchronize_session=False)
    db.commit()


def get_user_from_session_token(db: Session, token: str | None) -> AdminUser | None:
    if not token:
        return None

    now = _utc_now()
    token_digest = _token_hash(token)
    session = (
        db.query(AuthSession)
        .filter(AuthSession.token_hash == token_digest, AuthSession.expires_at > now)
        .first()
    )
    if not session:
        return None

    session.last_used_at = now
    db.commit()
    return db.query(AdminUser).filter(AdminUser.id == session.user_id).first()


def revoke_session(db: Session, token: str | None) -> None:
    if not token:
        return
    digest = _token_hash(token)
    db.query(AuthSession).filter(AuthSession.token_hash == digest).delete(synchronize_session=False)
    db.commit()
