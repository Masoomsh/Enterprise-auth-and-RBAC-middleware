from semantic_router.config import RoutingRule
from semantic_router.routing import ModelRouter, RoutingRuleEngine


def test_classifies_simple_reasoning_and_coding_queries():
    rules = RoutingRuleEngine()
    assert rules.classify("What time is it?") == "simple"
    assert rules.classify("Compare the trade-offs and explain why") == "reasoning"
    assert rules.classify("Debug this Python function") == "coding"


def test_custom_rules_are_checked_before_defaults():
    rules = RoutingRuleEngine((RoutingRule("reasoning", r"\bhello\b"),))
    assert rules.classify("Hello, write some code") == "reasoning"


def test_ab_assignment_and_cheapest_tier_routing():
    router = ModelRouter("small", "reasoner", "coder", "baseline", RoutingRuleEngine())
    treatment = router.route("Write code", "request-1", True, 100)
    control = router.route("Write code", "request-1", False, 100)
    assert (treatment.arm, treatment.model, treatment.category) == ("treatment", "coder", "coding")
    assert (control.arm, control.model) == ("control", "baseline")


def test_treatment_selects_lowest_cost_model_from_adequate_candidates():
    router = ModelRouter(
        "small", "reasoner", "coder", "baseline", RoutingRuleEngine(),
        model_candidates={"reasoning": ("premium", "economy")},
        model_prices={
            "premium": {"input_per_million": 8, "output_per_million": 20},
            "economy": {"input_per_million": 1, "output_per_million": 3},
        },
    )
    decision = router.route("Explain why this result follows", "request", True, 100)
    assert decision.model == "economy"
