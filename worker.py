import argparse
import asyncio
import json
import os
import random
from typing import Any

import httpx
import websockets


WORKER_TOKEN = os.getenv("WORKER_TOKEN", "worker-secret-token")

# OpenClaw/OpenAI-compatible upstream settings
OPENCLAW_CHAT_URL = os.getenv("OPENCLAW_CHAT_URL", "http://127.0.0.1:18789/v1/chat/completions")
OPENCLAW_API_KEY = os.getenv("OPENCLAW_API_KEY", "")
OPENCLAW_MODEL = os.getenv("OPENCLAW_MODEL", "openai-codex/gpt-5.3-codex")
OPENCLAW_TIMEOUT_SECONDS = float(os.getenv("OPENCLAW_TIMEOUT_SECONDS", "45"))
OPENCLAW_SESSION_HEADER = os.getenv("OPENCLAW_SESSION_HEADER", "x-openclaw-session-key")
OPENCLAW_SESSION_PREFIX = os.getenv("OPENCLAW_SESSION_PREFIX", "relay:")


def normalize_prefix(prefix: str) -> str:
    p = (prefix or "").strip()
    if not p:
        return ""
    if not p.startswith("/"):
        p = f"/{p}"
    return p.rstrip("/")


def _extract_text_from_chat_response(data: dict[str, Any]) -> str:
    choices = data.get("choices") or []
    if not choices:
        return ""
    msg = (choices[0] or {}).get("message") or {}
    content = msg.get("content")

    if isinstance(content, str):
        return content.strip()

    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                txt = item.get("text")
                if isinstance(txt, str):
                    parts.append(txt)
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(p for p in parts if p).strip()

    return ""


async def handle_task(message: str, worker_id: str, session_id: str) -> str:
    """
    Send the user message to local OpenClaw/OpenAI-compatible chat API and return model reply.
    If local upstream is unavailable, raise an explicit error (no fallback).
    """
    headers = {"Content-Type": "application/json"}
    if OPENCLAW_API_KEY:
        headers["Authorization"] = f"Bearer {OPENCLAW_API_KEY}"

    # Keep per-chat continuity by mapping relay session_id -> upstream session key header.
    if session_id:
        headers[OPENCLAW_SESSION_HEADER] = f"{OPENCLAW_SESSION_PREFIX}{session_id}"

    payload: dict[str, Any] = {
        "model": OPENCLAW_MODEL,
        "messages": [{"role": "user", "content": message}],
    }

    try:
        async with httpx.AsyncClient(timeout=OPENCLAW_TIMEOUT_SECONDS) as client:
            resp = await client.post(OPENCLAW_CHAT_URL, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
            text = _extract_text_from_chat_response(data)
            if text:
                return text
            return "[empty model response]"
    except Exception as exc:
        raise RuntimeError(f"local OpenClaw API unavailable: {exc}") from exc


async def run_worker(server_ws_url: str, worker_id: str, prefix: str = "/relay", unstable: bool = False) -> None:
    headers = {"Authorization": f"Bearer {WORKER_TOKEN}"}
    route_prefix = normalize_prefix(prefix)
    base = server_ws_url.rstrip("/")
    url = f"{base}{route_prefix}/ws/worker?worker_id={worker_id}"

    while True:
        try:
            async with websockets.connect(url, additional_headers=headers, ping_interval=20, ping_timeout=20) as ws:
                await ws.send(json.dumps({"type": "ready"}))
                async for raw in ws:
                    msg = json.loads(raw)
                    if msg.get("type") != "task":
                        continue

                    task_id = msg["task_id"]
                    content = msg["message"]
                    session_id = msg.get("session_id", "")

                    # Optional instability hook to exercise retry/timeout behavior.
                    if unstable and random.random() < 0.2:
                        await asyncio.sleep(999)

                    try:
                        result = await handle_task(content, worker_id, session_id)
                        await ws.send(
                            json.dumps(
                                {
                                    "type": "result",
                                    "task_id": task_id,
                                    "ok": True,
                                    "result": result,
                                }
                            )
                        )
                    except Exception as exc:
                        await ws.send(
                            json.dumps(
                                {
                                    "type": "result",
                                    "task_id": task_id,
                                    "ok": False,
                                    "error": str(exc),
                                }
                            )
                        )
                    finally:
                        await ws.send(json.dumps({"type": "ready"}))
        except Exception:
            await asyncio.sleep(1.0)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Prototype relay worker")
    parser.add_argument("--server", default="ws://127.0.0.1:18080", help="Base WS URL for relay server")
    parser.add_argument("--prefix", default=os.getenv("API_PREFIX", "/relay"), help="Subpath prefix, e.g. /relay")
    parser.add_argument("--worker-id", default="worker-local-1")
    parser.add_argument("--unstable", action="store_true", help="Enable random hangs for timeout/retry testing")
    args = parser.parse_args()

    asyncio.run(run_worker(args.server, args.worker_id, prefix=args.prefix, unstable=args.unstable))
