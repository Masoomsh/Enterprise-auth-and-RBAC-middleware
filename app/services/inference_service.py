"""Inference backend. Authorization has already happened by the time this is called."""
from typing import Awaitable, Callable

# (model, messages, tools, datasources) -> {"content": str, "usage": {...}}
InferenceBackend = Callable[[str, list[dict], list[str], list[str]], Awaitable[dict]]


async def litellm_backend(
    model: str, messages: list[dict], tools: list[str], datasources: list[str]
) -> dict:
    # Imported lazily: litellm is slow to import and not needed by the auth tests.
    import litellm

    # `tools` and `datasources` are authorized names only. Tool execution and data access
    # are performed by the gateway components that receive them, not by this service.
    response = await litellm.acompletion(model=model, messages=messages)
    usage = getattr(response, "usage", None)
    return {
        "content": response.choices[0].message.content,
        "usage": {
            "prompt_tokens": getattr(usage, "prompt_tokens", None),
            "completion_tokens": getattr(usage, "completion_tokens", None),
        },
    }


def get_inference_backend() -> InferenceBackend:
    return litellm_backend
