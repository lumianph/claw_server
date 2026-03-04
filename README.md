# claw_server

A local 3-part chat relay prototype built with Python/FastAPI.

## Components

- **A: Relay server (`server.py`)**
  - REST for clients: submit task + poll status
  - WebSocket endpoint for worker agents (`/ws/worker`)
  - In-memory task queue/state
  - Idempotency (`task_id` + `session_id`) handling
  - Basic timeout/retry for worker execution
  - Token + HMAC signature + timestamp + nonce replay protection for REST

- **B: Worker (`worker.py`)**
  - Outbound WebSocket connection to server
  - Receives tasks, calls local OpenClaw chat API, returns result

- **C: Client (`client.py`)**
  - Submits a task via REST
  - Polls for completion with timeout/retry behavior

## Ports / Isolation

- Uses `127.0.0.1:18080` by default (non-conflicting high port)
- No TLS/certs (HTTP + WS only), as requested for local prototype
- Runs in isolated folder: `relay-prototype`
- Does not touch OpenClaw services

## Quick Start

```bash
cd /root/.openclaw/workspace/relay-prototype
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Start server:

```bash
uvicorn server:app --host 127.0.0.1 --port 18080
```

In another terminal, start worker:

```bash
python worker.py --server ws://127.0.0.1:18080 --prefix /relay --worker-id worker-local-1
```

In another terminal, run client:

```bash
python client.py --base-url http://127.0.0.1:18080 --prefix /relay --session-id s1 --task-id t1 --message "hello world"
```

## Auth / Replay Protection

### REST client -> server

Headers required:

- `Authorization: Bearer <CLIENT_TOKEN>`
- `X-Timestamp: <unix-seconds>`
- `X-Nonce: <unique random nonce>`
- `X-Signature: <HMAC_SHA256(method + path + body + timestamp + nonce)>`

### Worker WS -> server

- `Authorization: Bearer <WORKER_TOKEN>` on WebSocket handshake

## Integration Test

```bash
python test_integration.py
```

Success output starts with:

```text
INTEGRATION_TEST_OK
```

## Security Caveats (Prototype)

- In-memory storage only (not durable)
- Single-process state; no shared cache/db for multi-instance deployments
- No TLS (credentials/signatures exposed if used beyond localhost)
- Nonce cache is process-local and non-persistent

## Next Steps for Production

1. Add TLS everywhere; enforce HTTPS/WSS.
2. Store task and nonce state in Redis/Postgres for durability and horizontal scale.
3. Use short-lived signed credentials + key rotation.
4. Add strict rate limiting and per-tenant isolation.
5. Add structured audit logs/metrics/tracing.
