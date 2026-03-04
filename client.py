import argparse
import hashlib
import hmac
import json
import os
import time
import uuid

import httpx


CLIENT_TOKEN = os.getenv("CLIENT_TOKEN", "client-secret-token")
SIGNING_SECRET = os.getenv("SIGNING_SECRET", "signing-secret")


def normalize_prefix(prefix: str) -> str:
    p = (prefix or "").strip()
    if not p:
        return ""
    if not p.startswith("/"):
        p = f"/{p}"
    return p.rstrip("/")


def make_sig(method: str, path: str, body: str, ts: str, nonce: str) -> str:
    payload = f"{method}\n{path}\n{body}\n{ts}\n{nonce}".encode()
    return hmac.new(SIGNING_SECRET.encode(), payload, hashlib.sha256).hexdigest()


def auth_headers(method: str, path: str, body: str = "") -> dict:
    ts = str(int(time.time()))
    nonce = uuid.uuid4().hex
    sig = make_sig(method, path, body, ts, nonce)
    return {
        "Authorization": f"Bearer {CLIENT_TOKEN}",
        "X-Timestamp": ts,
        "X-Nonce": nonce,
        "X-Signature": sig,
        "Content-Type": "application/json",
    }


def submit_and_wait(base_url: str, session_id: str, task_id: str, message: str, timeout_s: int = 20, prefix: str = "/relay") -> dict:
    route_prefix = normalize_prefix(prefix)
    submit_path = f"{route_prefix}/v1/tasks"
    payload = {"session_id": session_id, "task_id": task_id, "message": message}
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)

    with httpx.Client(timeout=5.0) as client:
        # Basic submit retry for transient failures.
        for attempt in range(3):
            try:
                r = client.post(f"{base_url.rstrip('/')}{submit_path}", headers=auth_headers("POST", submit_path, body), json=payload)
                if r.status_code in (200, 201):
                    break
                if r.status_code == 409:
                    break
            except httpx.HTTPError:
                if attempt == 2:
                    raise
                time.sleep(0.3 * (attempt + 1))
        else:
            raise RuntimeError("submit failed after retries")

        deadline = time.time() + timeout_s
        while time.time() < deadline:
            get_path = f"{route_prefix}/v1/tasks/{task_id}"
            gr = client.get(f"{base_url.rstrip('/')}{get_path}", headers=auth_headers("GET", get_path, ""))
            gr.raise_for_status()
            data = gr.json()
            if data["status"] in ("done", "failed"):
                return data
            time.sleep(0.5)

    raise TimeoutError("timed out waiting for task completion")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Prototype relay client")
    parser.add_argument("--base-url", default="http://127.0.0.1:18080")
    parser.add_argument("--prefix", default=os.getenv("API_PREFIX", "/relay"), help="Subpath prefix, e.g. /relay")
    parser.add_argument("--session-id", default="session-demo")
    parser.add_argument("--task-id", default=f"task-{uuid.uuid4().hex[:8]}")
    parser.add_argument("--message", required=True)
    parser.add_argument("--timeout", type=int, default=20)
    args = parser.parse_args()

    result = submit_and_wait(args.base_url, args.session_id, args.task_id, args.message, args.timeout, prefix=args.prefix)
    print(json.dumps(result, indent=2))
