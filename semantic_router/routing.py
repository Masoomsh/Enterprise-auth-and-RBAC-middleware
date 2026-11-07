from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Callable

from semantic_router.config import RoutingRule


@dataclass(frozen=True)
class RouteDecision:
    category: str
    model: str
    arm: str


class RoutingRuleEngine:
    def __init__(self, custom_rules: tuple[RoutingRule, ...] = ()) -> None:
        defaults = (
            RoutingRule("coding", r"\b(code|coding|program|programming|debug|refactor|"
                        r"function|class|typescript|javascript|python|sql|stack trace)\b"),
            RoutingRule("reasoning", r"\b(reason|reasoning|analy[sz]e|compare|trade-?off|"
                        r"prove|derive|step by step|evaluate|complex|why)\b"),
        )
        self._rules = tuple((rule.category, re.compile(rule.pattern, re.IGNORECASE))
                            for rule in (*custom_rules, *defaults))

    def classify(self, text: str) -> str:
        for category, pattern in self._rules:
            if pattern.search(text):
                return category
        return "simple"


class ModelRouter:
    def __init__(
        self,
        simple_model: str,
        reasoning_model: str,
        coding_model: str,
        baseline_model: str,
        rules: RoutingRuleEngine,
        model_candidates: dict[str, tuple[str, ...]] | None = None,
        model_prices: dict[str, dict[str, float]] | None = None,
    ) -> None:
        self._models = {
            "simple": simple_model,
            "reasoning": reasoning_model,
            "coding": coding_model,
        }
        self._baseline_model = baseline_model
        self._rules = rules
        self._candidates = model_candidates or {}
        self._prices = model_prices or {}

    def route(
        self,
        text: str,
        request_id: str,
        experiment_enabled: bool,
        treatment_percent: int,
        is_allowed: Callable[[str], bool] | None = None,
    ) -> RouteDecision:
        category = self._rules.classify(text)
        bucket = int(hashlib.sha256(request_id.encode("utf-8")).hexdigest()[:8], 16) % 100
        arm = "treatment" if experiment_enabled and bucket < treatment_percent else "control"
        model = self._cheapest_model(category) if arm == "treatment" else self._baseline_model
        if is_allowed is not None and not is_allowed(model):
            # The caller may not use the preferred model: fall back to the cheapest permitted one.
            permitted = [
                m for m in dict.fromkeys(
                    (*self._candidates.get(category, ()), self._models[category], self._baseline_model)
                )
                if is_allowed(m)
            ]
            if not permitted:
                raise PermissionError("No permitted model for this request")
            model = min(permitted, key=lambda m: sum(self.price(m, self._prices)))
        return RouteDecision(category=category, model=model, arm=arm)

    def _cheapest_model(self, category: str) -> str:
        candidates = self._candidates.get(category, (self._models[category],))
        return min(
            candidates,
            key=lambda model: sum(self.price(model, self._prices)),
        )

    def price(self, model: str, prices: dict[str, dict[str, float]]) -> tuple[float, float]:
        configured = prices.get(model, {})
        return (
            float(configured.get("input_per_million", 0)),
            float(configured.get("output_per_million", 0)),
        )
