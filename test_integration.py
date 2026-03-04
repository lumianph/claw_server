import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent


def wait_for_health(url: str, timeout: float = 15.0) -> None:
    end = time.time() + timeout
    while time.time() < end:
        try:
            r = httpx.get(f"{url}/healthz", timeout=1.5)
            if r.status_code == 200:
                return
        except Exception:
            pass
        time.sleep(0.3)
    raise RuntimeError("server did not become healthy")


def main() -> int:
    env = os.environ.copy()
    env.update(
        {
            "CLIENT_TOKEN": "client-secret-token",
            "WORKER_TOKEN": "worker-secret-token",
            "SIGNING_SECRET": "signing-secret",
            "TASK_TIMEOUT_SECONDS": "8",
            "MAX_RETRIES": "1",
        }
    )

    server_cmd = [sys.executable, "-m", "uvicorn", "server:app", "--host", "127.0.0.1", "--port", "18080"]
    worker_cmd = [sys.executable, "worker.py", "--server", "ws://127.0.0.1:18080", "--worker-id", "worker-it-1"]

    server = subprocess.Popen(server_cmd, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    worker = None
    try:
        wait_for_health("http://127.0.0.1:18080")
        worker = subprocess.Popen(worker_cmd, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        time.sleep(1.0)

        task_id = f"it-{uuid.uuid4().hex[:8]}"
        client_cmd = [
            sys.executable,
            "client.py",
            "--base-url",
            "http://127.0.0.1:18080",
            "--session-id",
            "session-it",
            "--task-id",
            task_id,
            "--message",
            "hello relay",
            "--timeout",
            "15",
        ]
        out = subprocess.check_output(client_cmd, cwd=ROOT, env=env, text=True)
        data = json.loads(out)

        assert data["status"] == "done", data
        assert "HELLO RELAY" in (data.get("result") or ""), data

        print("INTEGRATION_TEST_OK")
        print(json.dumps(data, indent=2))
        return 0
    finally:
        for proc in (worker, server):
            if not proc:
                continue
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
