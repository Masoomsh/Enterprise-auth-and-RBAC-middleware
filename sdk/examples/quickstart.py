"""Runs with no gateway:  python quickstart.py
Against the real gateway: GATEWAY_URL=... GATEWAY_API_KEY=gk_... python quickstart.py --remote
"""
import sys

from pydantic import BaseModel

from gateway_client import Client, GatewayError, PermissionDenied


class Ticket(BaseModel):
    title: str
    priority: int


def main() -> None:
    client = Client() if "--remote" in sys.argv else Client(mode="local")
    with client:
        print("models:", client.models())

        reply = client.chat("Explain semantic caching in one sentence")
        print(f"\n1. chat -> cache={reply.cache} model={reply.model}\n   {reply.content}")

        print("\n2. streaming -> ", end="")
        with client.chat("Write a haiku about caching", stream=True) as stream:
            for piece in stream:
                print(piece, end="", flush=True)
        print(f"\n   (cache={stream.cache}, tokens={stream.usage.get('total_tokens')})")

        ticket = client.chat("Create a ticket for the broken login page", response_model=Ticket)
        print(f"\n3. structured -> {ticket.parsed!r}")

        again = client.chat("Explain semantic caching in one sentence")
        print(f"\n4. same prompt again -> cache={again.cache}")

        try:
            client.chat("hello", model="a-model-i-may-not-use")
        except PermissionDenied as exc:
            print(f"\n5. denied as expected -> {exc}")
        except GatewayError as exc:
            print(f"\n5. {type(exc).__name__}: {exc}")
        else:
            print("\n5. (local mode has no RBAC, so nothing was denied; use --remote to see a 403)")


if __name__ == "__main__":
    main()
