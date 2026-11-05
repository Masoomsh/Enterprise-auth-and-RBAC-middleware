from scripts.generate_rbac_matrix import main, parse_policy


def test_rbac_matrix_doc_is_current():
    assert main(["--check"]) == 0, "run: python -m scripts.generate_rbac_matrix"


def test_policy_only_uses_known_roles():
    from app.services.identity_service import GATEWAY_ROLES

    unknown = {r for r, _, _ in parse_policy()} - set(GATEWAY_ROLES)
    assert not unknown, f"policy references roles SuperTokens does not manage: {unknown}"
