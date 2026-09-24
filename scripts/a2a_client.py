#!/usr/bin/env python3
"""Send one A2A message to the capability agent, the way a calling agent would, and print the reply.

    python scripts/a2a_client.py --url http://localhost:8080 --token dev-token "What capabilities do you have?"
    python scripts/a2a_client.py --url http://localhost:8080 --token dev-token --data '{"skill": "list_capabilities"}'
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from typing import Any

import httpx
from a2a.client import ClientConfig, create_client
from a2a.helpers.proto_helpers import get_data_parts, get_text_parts, new_data_part, new_message, new_text_part
from a2a.types.a2a_pb2 import Role, SendMessageRequest


async def send(url: str, token: str | None, text: str | None = None, data: dict[str, Any] | None = None,
               timeout: float = 300, insecure: bool = False) -> tuple[list[str], list[Any]]:
    """Discover the agent from its Agent Card, send one message, and return its text and data parts."""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    # Generous timeout: the first question after a new commit waits for the agent to re-crawl Git.
    async with httpx.AsyncClient(headers=headers, timeout=timeout, verify=not insecure) as http:
        client = await create_client(url, client_config=ClientConfig(streaming=False, httpx_client=http))
        part = new_text_part(text) if text is not None else new_data_part(data)
        request = SendMessageRequest(message=new_message([part], role=Role.ROLE_USER))
        texts, datas = [], []
        async for event in client.send_message(request):
            if event.HasField("message"):
                texts += get_text_parts(event.message.parts)
                datas += get_data_parts(event.message.parts)
        return texts, datas


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("question", nargs="?", help="Plain-text question")
    parser.add_argument("--data", help="JSON request instead of text, e.g. '{\"skill\": \"list_capabilities\"}'")
    parser.add_argument("--url", default=os.environ.get("CAPABILITY_AGENT_URL", "http://localhost:8080"))
    parser.add_argument("--token", default=os.environ.get("A2A_AUTH_TOKEN"))
    parser.add_argument("--quiet", action="store_true", help="Print only the text reply, not the JSON data")
    parser.add_argument("--insecure", action="store_true",
                        help="Skip TLS verification (the cluster ingress uses a self-signed certificate)")
    args = parser.parse_args()
    if not args.question and not args.data:
        parser.error("give a question or --data")

    texts, datas = asyncio.run(send(args.url, args.token, args.question, json.loads(args.data) if args.data else None,
                                    insecure=args.insecure))
    print("\n".join(texts))
    if datas and not args.quiet:
        print(json.dumps(datas[0], indent=2))


if __name__ == "__main__":
    main()
