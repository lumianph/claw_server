import argparse
import asyncio
import json
import os
import random

import websockets


WORKER_TOKEN = os.getenv("WORKER_TOKEN", "worker-secret-token")


def normalize_prefix(prefix: str) -> str:
    p = (prefix or "").strip()
    if not p:
        return ""
    if not p.startswith("/"):
        p = f"/{p}"
    return p.rstrip("/")


async def handle_task(message: str, worker_id: str) -> str:
    # Simulated processing logic.
    await asyncio.sleep(0.3)
    return f"[{worker_id}] {message.upper()}"


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

                    # Optional instability hook to exercise retry/timeout behavior.
                    if unstable and random.random() < 0.2:
                        await asyncio.sleep(999)

                    try:
                        result = await handle_task(content, worker_id)
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
