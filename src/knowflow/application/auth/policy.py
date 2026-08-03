from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from knowflow.domain.common.identity import payload_hash
from knowflow.infrastructure.db.models.identity import RoleCode


class Capability(StrEnum):
    KNOWLEDGE_QUERY = "knowledge.query"
    TICKET_CREATE = "ticket.create"
    TICKET_READ = "ticket.read"
    TICKET_UPDATE = "ticket.update"
    APPROVAL_DECIDE = "approval.decide"
    OPERATION_EXECUTE = "operation.execute"
    DOCUMENT_MANAGE = "document.manage"
    AUDIT_READ = "audit.read"
    RECOVERY_DECIDE = "recovery.decide"


ROLE_CAPABILITIES: dict[RoleCode, frozenset[Capability]] = {
    RoleCode.EMPLOYEE: frozenset(
        {Capability.KNOWLEDGE_QUERY, Capability.TICKET_CREATE, Capability.TICKET_READ}
    ),
    RoleCode.OPERATOR: frozenset(
        {
            Capability.KNOWLEDGE_QUERY,
            Capability.TICKET_CREATE,
            Capability.TICKET_READ,
            Capability.TICKET_UPDATE,
            Capability.AUDIT_READ,
            Capability.RECOVERY_DECIDE,
        }
    ),
    RoleCode.APPROVER: frozenset(
        {Capability.KNOWLEDGE_QUERY, Capability.TICKET_READ, Capability.APPROVAL_DECIDE}
    ),
    RoleCode.ADMIN: frozenset(Capability),
}


@dataclass(frozen=True, slots=True)
class AccessContext:
    user_id: str
    session_id: str
    roles: frozenset[RoleCode]
    team_id: str | None
    acl_version: int

    def __post_init__(self) -> None:
        if not self.user_id or not self.session_id:
            raise ValueError("trusted identity and session are required")
        if not self.roles:
            raise ValueError("at least one server-derived role is required")
        if self.acl_version < 1:
            raise ValueError("acl_version must be positive")

    @property
    def capabilities(self) -> frozenset[Capability]:
        return frozenset().union(*(ROLE_CAPABILITIES[role] for role in self.roles))

    def acl_scope_tokens(self) -> tuple[str, ...]:
        tokens = {"public", f"user:{self.user_id}"}
        tokens.update(f"role:{role.value}" for role in self.roles)
        if self.team_id:
            tokens.add(f"team:{self.team_id}")
        return tuple(sorted(tokens))

    def scope_fingerprint(self) -> str:
        return payload_hash(
            {
                "user_id": self.user_id,
                "roles": sorted(role.value for role in self.roles),
                "team_id": self.team_id,
                "acl_version": self.acl_version,
            }
        )


def require_capability(context: AccessContext, capability: Capability) -> None:
    if capability not in context.capabilities:
        raise PermissionError(f"capability {capability.value} is not granted")


def ticket_is_visible(
    context: AccessContext, *, owner_user_id: str, assigned_team_id: str | None
) -> bool:
    if RoleCode.ADMIN in context.roles:
        return True
    if owner_user_id == context.user_id:
        return True
    return (
        RoleCode.OPERATOR in context.roles
        and context.team_id is not None
        and context.team_id == assigned_team_id
    )


def workflow_is_visible(context: AccessContext, *, owner_user_id: str) -> bool:
    return owner_user_id == context.user_id or bool(
        context.roles.intersection({RoleCode.OPERATOR, RoleCode.ADMIN})
    )
