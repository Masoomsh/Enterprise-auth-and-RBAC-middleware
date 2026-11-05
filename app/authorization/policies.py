from functools import lru_cache

from app.authorization.rbac import RBACEngine
from app.config.settings import BASE_DIR

POLICY_DIR = BASE_DIR / "policies"
MODEL_PATH = POLICY_DIR / "rbac_model.conf"
POLICY_PATH = POLICY_DIR / "rbac_policy.csv"


@lru_cache(maxsize=1)
def get_engine() -> RBACEngine:
    return RBACEngine(MODEL_PATH, POLICY_PATH)
