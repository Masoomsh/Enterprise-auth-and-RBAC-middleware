"""Resource taxonomy shared by RBAC policy, token scopes and the audit log.

A concrete resource is `<type>:<id>` (e.g. `model:gpt-4o`, `tool:web-search`,
`datasource:finance-dw`, `admin:roles`, `endpoint:reports`). Policies and scopes use glob patterns over the same
string (`model:small-*`, `tool:*`, `*`). Scopes narrow by resource only; the action is limited
by the role.
"""
import re
from fnmatch import fnmatchcase
from typing import Iterable

RESOURCE_TYPES = ("model", "tool", "datasource", "admin", "endpoint")
ACTIONS = ("invoke", "read", "write", "admin")

# Concrete resources coming from requests must NOT contain wildcards, otherwise a caller
# could ask for `model:*` and match every pattern.
_CONCRETE = re.compile(
    r"^(" + "|".join(RESOURCE_TYPES) + r"):[A-Za-z0-9][A-Za-z0-9._:/\-]{0,127}$"
)


def concrete_resource(resource_type: str, resource_id: str) -> str:
    resource = f"{resource_type}:{resource_id}"
    if not _CONCRETE.match(resource):
        raise ValueError(f"Invalid resource identifier: {resource!r}")
    return resource


_SCOPE = re.compile(
    r"^(\*|(" + "|".join(RESOURCE_TYPES) + r"):[A-Za-z0-9._:/*\-]{1,128})$"
)


def is_valid_scope(scope: str) -> bool:
    return bool(_SCOPE.match(scope))


def is_concrete(resource: str) -> bool:
    return bool(_CONCRETE.match(resource))


def pattern_matches(pattern: str, resource: str) -> bool:
    return pattern == "*" or fnmatchcase(resource, pattern)


def scopes_allow(scopes: Iterable[str], resource: str) -> bool:
    return any(pattern_matches(scope, resource) for scope in scopes)
