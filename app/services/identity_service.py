from supertokens_python.recipe.userroles.asyncio import (
    add_role_to_user,
    create_new_role_or_add_permissions,
    get_roles_for_user,
    remove_user_role,
)

# Must match the roles in the gateway's policies/rbac_policy.csv.
GATEWAY_ROLES = ("admin", "engineer", "analyst", "viewer")


async def ensure_roles() -> None:
    for role in GATEWAY_ROLES:
        await create_new_role_or_add_permissions(role, [])


async def assign_role(user_id: str, role: str, tenant_id: str = "public") -> None:
    if role not in GATEWAY_ROLES:
        raise ValueError(f"Unknown role {role!r}; expected one of {', '.join(GATEWAY_ROLES)}")
    await ensure_roles()
    result = await add_role_to_user(tenant_id, user_id, role)
    if type(result).__name__ == "UnknownRoleError":
        raise RuntimeError(f"Role {role!r} does not exist")


async def revoke_role(user_id: str, role: str, tenant_id: str = "public") -> None:
    await remove_user_role(tenant_id, user_id, role)


async def owner_roles(subject: str, auth_method: str) -> frozenset[str] | None:
    """Current roles of an API key's owner, or None if they cannot be looked up.

    Only SuperTokens users can be checked live. Roles held via an IdP token live in the IdP, so
    keys owned by such identities are treated as unverifiable (and are not created).
    """
    if auth_method != "session":
        return None
    return frozenset(await list_roles(subject))


async def list_roles(user_id: str, tenant_id: str = "public") -> list[str]:
    return list((await get_roles_for_user(tenant_id, user_id)).roles)
