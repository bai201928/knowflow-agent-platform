from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import jwt
from pwdlib import PasswordHash

from knowflow.config import Settings
from knowflow.infrastructure.db.models.identity import RoleCode


class InvalidTokenError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class TrustedClaims:
    user_id: str
    session_id: str
    roles: tuple[RoleCode, ...]
    acl_version: int
    issued_at: datetime
    expires_at: datetime
    token_id: str


class AuthService:
    """Password and JWT primitives; session state is rechecked by the request dependency."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._password_hash = PasswordHash.recommended()

    def hash_password(self, password: str) -> str:
        if len(password) < 8:
            raise ValueError("password must contain at least eight characters")
        return self._password_hash.hash(password)

    def verify_password(self, password: str, encoded_hash: str) -> bool:
        try:
            return self._password_hash.verify(password, encoded_hash)
        except (ValueError, TypeError):
            return False

    def issue_access_token(
        self,
        *,
        user_id: str,
        session_id: str,
        roles: tuple[RoleCode, ...],
        acl_version: int,
        now: datetime | None = None,
    ) -> str:
        issued_at = _as_utc(now or datetime.now(UTC))
        expires_at = issued_at + timedelta(seconds=self._settings.jwt_ttl_seconds)
        payload: dict[str, Any] = {
            "sub": user_id,
            "sid": session_id,
            "roles": sorted(role.value for role in roles),
            "acl": acl_version,
            "iss": self._settings.jwt_issuer,
            "aud": self._settings.jwt_audience,
            "iat": int(issued_at.timestamp()),
            "exp": int(expires_at.timestamp()),
            "jti": str(uuid4()),
        }
        return jwt.encode(
            payload,
            self._settings.jwt_secret.get_secret_value(),
            algorithm="HS256",
        )

    def decode_access_token(self, token: str, *, now: datetime | None = None) -> TrustedClaims:
        try:
            payload = jwt.decode(
                token,
                self._settings.jwt_secret.get_secret_value(),
                algorithms=["HS256"],
                audience=self._settings.jwt_audience,
                issuer=self._settings.jwt_issuer,
                options={
                    "verify_exp": False,
                    "require": ["sub", "sid", "roles", "acl", "iat", "exp", "jti"],
                },
            )
            checked_at = _as_utc(now or datetime.now(UTC))
            issued_at = datetime.fromtimestamp(int(payload["iat"]), UTC)
            expires_at = datetime.fromtimestamp(int(payload["exp"]), UTC)
            if expires_at <= checked_at or issued_at > checked_at + timedelta(seconds=30):
                raise InvalidTokenError("access token is expired or not yet valid")
            roles = tuple(RoleCode(value) for value in payload["roles"])
            acl_version = int(payload["acl"])
            if acl_version < 1:
                raise InvalidTokenError("invalid ACL version")
            return TrustedClaims(
                user_id=str(payload["sub"]),
                session_id=str(payload["sid"]),
                roles=roles,
                acl_version=acl_version,
                issued_at=issued_at,
                expires_at=expires_at,
                token_id=str(payload["jti"]),
            )
        except InvalidTokenError:
            raise
        except (jwt.PyJWTError, KeyError, TypeError, ValueError) as exc:
            raise InvalidTokenError("invalid access token") from exc


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("security timestamps must be timezone-aware")
    return value.astimezone(UTC)
