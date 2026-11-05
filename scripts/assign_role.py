r"""Assign a gateway role to a SuperTokens user.

Usage (from the repository root):
    .\.venv\Scripts\python.exe -m scripts.assign_role <user_id> <role>

The user must sign in again (or refresh the session) to get the role in a new access token.
"""
import asyncio
import sys

from app.authentication.supertokens import configure_supertokens
from app.services.identity_service import GATEWAY_ROLES, assign_role, list_roles


def main() -> None:
    if len(sys.argv) != 3 or sys.argv[2] not in GATEWAY_ROLES:
        raise SystemExit(f"usage: assign_role <user_id> <{'|'.join(GATEWAY_ROLES)}>")
    user_id, role = sys.argv[1], sys.argv[2]
    configure_supertokens()

    async def run() -> list[str]:
        await assign_role(user_id, role)
        return await list_roles(user_id)

    print(f"Roles for {user_id}: {', '.join(asyncio.run(run()))}")


if __name__ == "__main__":
    main()
