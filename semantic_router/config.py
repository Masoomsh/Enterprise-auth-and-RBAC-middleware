from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from dotenv import load_dotenv


@dataclass(frozen=True)
class RoutingRule:
    category: str
    pattern: str


@dataclass(frozen=True)
class Settings:
    pinecone_api_key: str = ""
    pinecone_index: str = "semantic-cache"
    pinecone_namespace: str = "semantic-cache"
    similarity_threshold: float = 0.90
    ollama_base_url: str = "http://localhost:11434"
    embedding_model: str = "nomic-embed-text"
    simple_model: str = "llama3.2:latest"
    reasoning_model: str = "qwen2.5:3b"
    coding_model: str = "qwen2.5-coder:7b"
    baseline_model: str = "llama3.2:latest"
    cache_ttl_seconds: int = 3600
    cache_max_response_chars: int = 40000
    ab_test_enabled: bool = False
    ab_test_treatment_percent: int = 50
    model_prices: dict[str, dict[str, float]] = field(default_factory=dict)
    model_candidates: dict[str, tuple[str, ...]] = field(default_factory=dict)
    metrics_db_path: str = "./data/metrics.sqlite3"
    routing_rules: tuple[RoutingRule, ...] = ()

    @classmethod
    def from_env(cls) -> Settings:
        load_dotenv()
        raw_prices = os.getenv("MODEL_PRICES_JSON", "{}")
        raw_candidates = os.getenv("MODEL_CANDIDATES_JSON", "{}")
        raw_rules = os.getenv("ROUTING_RULES_JSON", "")
        try:
            prices = json.loads(raw_prices)
            if not isinstance(prices, dict):
                raise ValueError("MODEL_PRICES_JSON must be a JSON object")
            candidates_data = json.loads(raw_candidates)
            if not isinstance(candidates_data, dict):
                raise ValueError("MODEL_CANDIDATES_JSON must be a JSON object")
            if any(
                not isinstance(models, list)
                or any(not isinstance(model, str) or not model for model in models)
                for models in candidates_data.values()
            ):
                raise ValueError("Each model candidate category must contain an array of model names")
            candidates = {
                category: tuple(models)
                for category, models in candidates_data.items()
            }
            rules_data = json.loads(raw_rules) if raw_rules else []
            if not isinstance(rules_data, list):
                raise ValueError("ROUTING_RULES_JSON must be a JSON array")
            rules = tuple(
                RoutingRule(category=item["category"], pattern=item["pattern"])
                for item in rules_data
            )
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise ValueError(f"Invalid model pricing or routing rule configuration: {exc}") from exc

        return cls(
            pinecone_api_key=os.getenv("PINECONE_API_KEY", ""),
            pinecone_index=os.getenv("PINECONE_INDEX", "semantic-cache"),
            pinecone_namespace=os.getenv("PINECONE_NAMESPACE", "semantic-cache"),
            similarity_threshold=float(os.getenv("PINECONE_SIMILARITY_THRESHOLD", "0.90")),
            ollama_base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
            embedding_model=os.getenv("OLLAMA_EMBEDDING_MODEL", "nomic-embed-text"),
            simple_model=os.getenv("OLLAMA_SIMPLE_MODEL", "llama3.2:latest"),
            reasoning_model=os.getenv("OLLAMA_REASONING_MODEL", "qwen2.5:3b"),
            coding_model=os.getenv("OLLAMA_CODING_MODEL", "qwen2.5-coder:7b"),
            baseline_model=os.getenv("OLLAMA_BASELINE_MODEL", "llama3.2:latest"),
            cache_ttl_seconds=int(os.getenv("CACHE_TTL_SECONDS", "3600")),
            cache_max_response_chars=int(os.getenv("CACHE_MAX_RESPONSE_CHARS", "40000")),
            ab_test_enabled=os.getenv("AB_TEST_ENABLED", "false").lower() in {"1", "true", "yes"},
            ab_test_treatment_percent=int(os.getenv("AB_TEST_TREATMENT_PERCENT", "50")),
            model_prices=prices,
            model_candidates=candidates,
            metrics_db_path=os.getenv("METRICS_DB_PATH", "./data/metrics.sqlite3"),
            routing_rules=rules,
        )

    def validate(self) -> None:
        if not 0 <= self.similarity_threshold <= 1:
            raise ValueError("PINECONE_SIMILARITY_THRESHOLD must be between 0 and 1")
        if self.cache_ttl_seconds <= 0 or self.cache_max_response_chars <= 0:
            raise ValueError("Cache TTL and maximum response size must be positive")
        if not 0 <= self.ab_test_treatment_percent <= 100:
            raise ValueError("AB_TEST_TREATMENT_PERCENT must be between 0 and 100")
        for rule in self.routing_rules:
            if rule.category not in {"simple", "reasoning", "coding"}:
                raise ValueError(f"Unsupported routing category: {rule.category}")
        for category, models in self.model_candidates.items():
            if category not in {"simple", "reasoning", "coding"}:
                raise ValueError(f"Unsupported model candidate category: {category}")
            if (
                not isinstance(models, (tuple, list))
                or not models
                or any(not isinstance(model, str) or not model for model in models)
            ):
                raise ValueError(f"Model candidates for {category} must be non-empty model names")
            if any(model not in self.model_prices for model in models):
                raise ValueError(f"MODEL_PRICES_JSON must include every {category} candidate model")
        for model, rates in self.model_prices.items():
            if not isinstance(rates, dict) or any(
                key not in {"input_per_million", "output_per_million"}
                or not isinstance(value, (int, float))
                or value < 0
                for key, value in rates.items()
            ):
                raise ValueError(f"Invalid pricing configuration for model {model}")
