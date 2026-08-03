from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from knowflow.application.auth.policy import (
    AccessContext,
    Capability,
    require_capability,
    ticket_is_visible,
)
from knowflow.application.auth.service import AuthService, InvalidTokenError
from knowflow.config import Settings
from knowflow.infrastructure.db.models.identity import RoleCode

NOW = datetime(2026, 8, 3, 5, 0, tzinfo=UTC)


def _service() -> AuthService:
    return AuthService(Settings(jwt_secret="unit-test-jwt-secret-that-is-at-least-32-characters"))


def test_argon2_password_verification_never_accepts_wrong_password() -> None:
    service = _service()
    encoded = service.hash_password("correct horse battery staple")
    assert encoded.startswith("$argon2")
    assert service.verify_password("correct horse battery staple", encoded)
    assert not service.verify_password("wrong", encoded)


def test_access_token_round_trip_uses_only_trusted_claims() -> None:
    service = _service()
    token = service.issue_access_token(
        user_id="user-1",
        session_id="session-1",
        roles=(RoleCode.EMPLOYEE,),
        acl_version=4,
        now=NOW,
    )
    claims = service.decode_access_token(token, now=NOW + timedelta(seconds=1))
    assert claims.user_id == "user-1"
    assert claims.session_id == "session-1"
    assert claims.roles == (RoleCode.EMPLOYEE,)
    assert claims.acl_version == 4


def test_tampered_or_expired_access_token_is_rejected() -> None:
    service = _service()
    token = service.issue_access_token(
        user_id="user-1",
        session_id="session-1",
        roles=(RoleCode.EMPLOYEE,),
        acl_version=1,
        now=NOW,
    )
    with pytest.raises(InvalidTokenError):
        service.decode_access_token(f"{token[:-1]}x", now=NOW + timedelta(seconds=1))
    with pytest.raises(InvalidTokenError):
        service.decode_access_token(token, now=NOW + timedelta(hours=1))


def test_access_context_is_immutable_and_has_server_role_capabilities() -> None:
    employee = AccessContext(
        user_id="u1",
        session_id="s1",
        roles=frozenset({RoleCode.EMPLOYEE}),
        team_id="team-a",
        acl_version=2,
    )
    require_capability(employee, Capability.KNOWLEDGE_QUERY)
    require_capability(employee, Capability.TICKET_CREATE)
    with pytest.raises(PermissionError):
        require_capability(employee, Capability.APPROVAL_DECIDE)
    with pytest.raises(AttributeError):
        employee.user_id = "attacker"  # type: ignore[misc]


def test_ticket_visibility_uses_owner_team_and_privileged_role() -> None:
    employee = AccessContext("u1", "s1", frozenset({RoleCode.EMPLOYEE}), "team-a", 1)
    operator = AccessContext("u2", "s2", frozenset({RoleCode.OPERATOR}), "team-a", 1)
    admin = AccessContext("u3", "s3", frozenset({RoleCode.ADMIN}), None, 1)

    assert ticket_is_visible(employee, owner_user_id="u1", assigned_team_id="team-b")
    assert not ticket_is_visible(employee, owner_user_id="other", assigned_team_id="team-a")
    assert ticket_is_visible(operator, owner_user_id="other", assigned_team_id="team-a")
    assert not ticket_is_visible(operator, owner_user_id="other", assigned_team_id="team-b")
    assert ticket_is_visible(admin, owner_user_id="other", assigned_team_id="team-b")


def test_acl_scope_tokens_are_stable_and_identity_bound() -> None:
    context = AccessContext(
        "u1",
        "s1",
        frozenset({RoleCode.OPERATOR, RoleCode.EMPLOYEE}),
        "team-a",
        7,
    )
    assert context.acl_scope_tokens() == (
        "public",
        "role:EMPLOYEE",
        "role:OPERATOR",
        "team:team-a",
        "user:u1",
    )
    assert (
        context.scope_fingerprint()
        != AccessContext("u1", "s1", context.roles, "team-a", 8).scope_fingerprint()
    )
