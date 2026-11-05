"""RBAC decision engine: casbin role policy ∩ token/key scopes, default deny."""
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import casbin

from app.authentication.base import Identity
from app.authorization.permissions import ACTIONS, is_concrete, scopes_allow


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str
    role: Optional[str] = None  # the role that granted access, when allowed


class RBACEngine:
    def __init__(self, model_path: Path, policy_path: Path):
        self._model_path = str(model_path)
        self._policy_path = str(policy_path)
        self._enforcer = casbin.Enforcer(self._model_path, self._policy_path)

    def reload(self) -> None:
        self._enforcer.load_policy()

    def check(self, identity: Identity, resource: str, action: str) -> Decision:
        """Effective permission = (any role allows) AND (resource within token/key scopes)."""
        if action not in ACTIONS:
            return Decision(False, f"unknown action {action!r}")
        if not is_concrete(resource):
            return Decision(False, "invalid resource identifier")
        if not identity.roles:
            return Decision(False, "identity has no roles")

        granting_role = next(
            (r for r in sorted(identity.roles) if self._enforcer.enforce(r, resource, action)),
            None,
        )
        if granting_role is None:
            return Decision(False, "no role grants this action on this resource")

        # Empty scopes = no narrowing (sessions). API keys can never have empty scopes.
        if identity.scopes and not scopes_allow(identity.scopes, resource):
            return Decision(False, "resource is outside the token scope")

        return Decision(True, "allowed by role policy", role=granting_role)
