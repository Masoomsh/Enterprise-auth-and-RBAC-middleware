import pytest

from app.authentication.base import Identity
from app.authorization.policies import get_engine


def ident(*roles, scopes=()):
    return Identity(
        subject="u1",
        tenant="default",
        auth_method="session",
        roles=frozenset(roles),
        scopes=frozenset(scopes),
    )


# (role, resource, action, expected). This table IS the RBAC matrix.
MATRIX = [
    # admin: everything, including admin resources
    ("admin", "model:gpt-4o", "invoke", True),
    ("admin", "tool:anything", "invoke", True),
    ("admin", "datasource:finance-dw", "read", True),
    ("admin", "admin:roles", "write", True),
    # engineer: all models/tools, read data; no writes, no admin
    ("engineer", "model:gpt-4o", "invoke", True),
    ("engineer", "tool:web-search", "invoke", True),
    ("engineer", "datasource:finance-dw", "read", True),
    ("engineer", "datasource:finance-dw", "write", False),
    ("engineer", "admin:roles", "write", False),
    ("engineer", "model:gpt-4o", "admin", False),
    # analyst: small models, two tools, analytics data only
    ("analyst", "model:small-1", "invoke", True),
    ("analyst", "model:gpt-4o", "invoke", False),
    ("analyst", "tool:web-search", "invoke", True),
    ("analyst", "tool:sql-query", "invoke", True),
    ("analyst", "tool:shell", "invoke", False),
    ("analyst", "datasource:analytics-eu", "read", True),
    ("analyst", "datasource:finance-dw", "read", False),
    ("analyst", "datasource:analytics-eu", "write", False),
    # viewer: small models only
    ("viewer", "model:small-1", "invoke", True),
    ("viewer", "model:gpt-4o", "invoke", False),
    ("viewer", "tool:web-search", "invoke", False),
    ("viewer", "datasource:analytics-eu", "read", False),
    # unknown role grants nothing
    ("intern", "model:small-1", "invoke", False),
]


@pytest.mark.parametrize("role,resource,action,expected", MATRIX)
def test_role_matrix(role, resource, action, expected):
    assert get_engine().check(ident(role), resource, action).allowed is expected


def test_no_roles_denied():
    d = get_engine().check(ident(), "model:small-1", "invoke")
    assert not d.allowed and "no roles" in d.reason


def test_any_granting_role_is_enough():
    d = get_engine().check(ident("viewer", "engineer"), "model:gpt-4o", "invoke")
    assert d.allowed and d.role == "engineer"


@pytest.mark.parametrize(
    "resource",
    ["model:*", "model:", "gpt-4o", "model:../etc", "*", "model:a b", "unknown:thing", ""],
)
def test_wildcard_or_malformed_resource_denied_even_for_admin(resource):
    assert not get_engine().check(ident("admin"), resource, "invoke").allowed


def test_unknown_action_denied_even_for_admin():
    assert not get_engine().check(ident("admin"), "model:gpt-4o", "delete").allowed


def test_scopes_narrow_role_permissions():
    admin_key = ident("admin", scopes=["model:small-*"])
    engine = get_engine()
    assert engine.check(admin_key, "model:small-1", "invoke").allowed
    d = engine.check(admin_key, "model:gpt-4o", "invoke")
    assert not d.allowed and "scope" in d.reason


def test_scopes_cannot_widen_role_permissions():
    viewer_key = ident("viewer", scopes=["*"])
    assert not get_engine().check(viewer_key, "model:gpt-4o", "invoke").allowed
