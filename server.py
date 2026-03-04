import asyncio
import hashlib
import hmac
import json
import os
import time
import uuid
from collections import deque
from dataclasses import dataclass
from typing import Deque, Dict, Optional

from fastapi import FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field


CLIENT_TOKEN = os.getenv("CLIENT_TOKEN", "client-secret-token")
WORKER_TOKEN = os.getenv("WORKER_TOKEN", "worker-secret-token")
SIGNING_SECRET = os.getenv("SIGNING_SECRET", "signing-secret")
MAX_SKEW_SECONDS = int(os.getenv("MAX_SKEW_SECONDS", "60"))
NONCE_TTL_SECONDS = int(os.getenv("NONCE_TTL_SECONDS", "300"))
TASK_TIMEOUT_SECONDS = int(os.getenv("TASK_TIMEOUT_SECONDS", "15"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "2"))
DISPATCH_INTERVAL_SECONDS = float(os.getenv("DISPATCH_INTERVAL_SECONDS", "0.25"))

# Supports mounting under a subpath, e.g. /relay
API_PREFIX = os.getenv("API_PREFIX", "/relay").strip()
if not API_PREFIX.startswith("/"):
    API_PREFIX = f"/{API_PREFIX}"
API_PREFIX = API_PREFIX.rstrip("/")
if API_PREFIX == "/":
    API_PREFIX = ""


class SubmitTaskRequest(BaseModel):
    session_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    message: str = Field(min_length=1)


class TaskStatusResponse(BaseModel):
    task_id: str
    session_id: str
    status: str
    result: Optional[str] = None
    error: Optional[str] = None
    retries: int = 0
    worker_id: Optional[str] = None


@dataclass
class TaskRecord:
    session_id: str
    task_id: str
    message: str
    status: str = "pending"
    result: Optional[str] = None
    error: Optional[str] = None
    retries: int = 0
    worker_id: Optional[str] = None
    assigned_at: Optional[float] = None


@dataclass
class WorkerConn:
    worker_id: str
    websocket: WebSocket
    busy: bool = False
    current_task_id: Optional[str] = None


app = FastAPI(title="Relay Prototype")

_tasks: Dict[str, TaskRecord] = {}
_pending: Deque[str] = deque()
_workers: Dict[str, WorkerConn] = {}
_seen_nonces: Dict[str, float] = {}
_lock = asyncio.Lock()


def _cleanup_nonces(now: float) -> None:
    expired = [nonce for nonce, ts in _seen_nonces.items() if now - ts > NONCE_TTL_SECONDS]
    for nonce in expired:
        _seen_nonces.pop(nonce, None)


def _hmac_signature(method: str, path: str, body: str, ts: str, nonce: str) -> str:
    payload = f"{method}\n{path}\n{body}\n{ts}\n{nonce}".encode()
    return hmac.new(SIGNING_SECRET.encode(), payload, hashlib.sha256).hexdigest()


def _verify_rest_auth(
    authorization: Optional[str],
    x_timestamp: Optional[str],
    x_nonce: Optional[str],
    x_signature: Optional[str],
    method: str,
    path: str,
    body: str,
) -> None:
    if authorization != f"Bearer {CLIENT_TOKEN}":
        raise HTTPException(status_code=401, detail="invalid bearer token")
    if not x_timestamp or not x_nonce or not x_signature:
        raise HTTPException(status_code=401, detail="missing auth headers")

    try:
        ts_val = int(x_timestamp)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="invalid timestamp") from exc

    now = int(time.time())
    if abs(now - ts_val) > MAX_SKEW_SECONDS:
        raise HTTPException(status_code=401, detail="timestamp outside allowed skew")

    expected = _hmac_signature(method, path, body, x_timestamp, x_nonce)
    if not hmac.compare_digest(expected, x_signature):
        raise HTTPException(status_code=401, detail="invalid signature")

    _cleanup_nonces(now)
    if x_nonce in _seen_nonces:
        raise HTTPException(status_code=409, detail="replay nonce detected")
    _seen_nonces[x_nonce] = float(now)


@app.on_event("startup")
async def startup() -> None:
    app.state.dispatch_task = asyncio.create_task(_dispatch_loop())
    app.state.timeout_task = asyncio.create_task(_timeout_loop())


@app.on_event("shutdown")
async def shutdown() -> None:
    for task_name in ("dispatch_task", "timeout_task"):
        task = getattr(app.state, task_name, None)
        if task:
            task.cancel()
    await asyncio.sleep(0)


@app.get("/healthz")
@app.get(f"{API_PREFIX}/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "prefix": API_PREFIX or "/"}


@app.post(f"{API_PREFIX}/v1/tasks", response_model=TaskStatusResponse)
async def submit_task(
    req: SubmitTaskRequest,
    authorization: Optional[str] = Header(default=None),
    x_timestamp: Optional[str] = Header(default=None),
    x_nonce: Optional[str] = Header(default=None),
    x_signature: Optional[str] = Header(default=None),
):
    body = req.model_dump_json()
    submit_path = f"{API_PREFIX}/v1/tasks"
    _verify_rest_auth(authorization, x_timestamp, x_nonce, x_signature, "POST", submit_path, body)

    async with _lock:
        existing = _tasks.get(req.task_id)
        if existing:
            if existing.session_id != req.session_id:
                raise HTTPException(status_code=409, detail="task_id already used by another session")
            return TaskStatusResponse(**existing.__dict__)

        record = TaskRecord(session_id=req.session_id, task_id=req.task_id, message=req.message)
        _tasks[req.task_id] = record
        _pending.append(req.task_id)
        return TaskStatusResponse(**record.__dict__)


@app.get(f"{API_PREFIX}/v1/tasks/{{task_id}}", response_model=TaskStatusResponse)
async def get_task(
    task_id: str,
    authorization: Optional[str] = Header(default=None),
    x_timestamp: Optional[str] = Header(default=None),
    x_nonce: Optional[str] = Header(default=None),
    x_signature: Optional[str] = Header(default=None),
):
    get_path = f"{API_PREFIX}/v1/tasks/{task_id}"
    _verify_rest_auth(authorization, x_timestamp, x_nonce, x_signature, "GET", get_path, "")

    async with _lock:
        record = _tasks.get(task_id)
        if not record:
            raise HTTPException(status_code=404, detail="task not found")
        return TaskStatusResponse(**record.__dict__)


@app.websocket(f"{API_PREFIX}/ws/worker")
async def worker_ws(websocket: WebSocket):
    auth = websocket.headers.get("authorization")
    worker_id = websocket.query_params.get("worker_id") or f"worker-{uuid.uuid4().hex[:8]}"
    if auth != f"Bearer {WORKER_TOKEN}":
        await websocket.close(code=4401)
        return

    await websocket.accept()
    conn = WorkerConn(worker_id=worker_id, websocket=websocket)
    async with _lock:
        _workers[worker_id] = conn

    try:
        while True:
            msg = await websocket.receive_text()
            data = json.loads(msg)
            msg_type = data.get("type")
            if msg_type == "ready":
                async with _lock:
                    if worker_id in _workers:
                        _workers[worker_id].busy = False
                        _workers[worker_id].current_task_id = None
            elif msg_type == "result":
                task_id = data.get("task_id")
                ok = data.get("ok", False)
                result = data.get("result")
                error = data.get("error")
                async with _lock:
                    rec = _tasks.get(task_id)
                    if rec and rec.status == "processing":
                        rec.status = "done" if ok else "failed"
                        rec.result = result if ok else None
                        rec.error = error if not ok else None
                        rec.assigned_at = None
                    if worker_id in _workers:
                        _workers[worker_id].busy = False
                        _workers[worker_id].current_task_id = None
    except WebSocketDisconnect:
        pass
    finally:
        async with _lock:
            conn = _workers.pop(worker_id, None)
            if conn and conn.current_task_id:
                rec = _tasks.get(conn.current_task_id)
                if rec and rec.status == "processing":
                    rec.status = "pending"
                    rec.assigned_at = None
                    _pending.appendleft(rec.task_id)


async def _dispatch_loop() -> None:
    while True:
        async with _lock:
            available = [w for w in _workers.values() if not w.busy]
            while _pending and available:
                worker = available.pop(0)
                task_id = _pending.popleft()
                rec = _tasks.get(task_id)
                if not rec or rec.status != "pending":
                    continue
                rec.status = "processing"
                rec.worker_id = worker.worker_id
                rec.assigned_at = time.time()
                worker.busy = True
                worker.current_task_id = task_id
                payload = {
                    "type": "task",
                    "task_id": rec.task_id,
                    "session_id": rec.session_id,
                    "message": rec.message,
                    "retries": rec.retries,
                }
                await worker.websocket.send_text(json.dumps(payload))
        await asyncio.sleep(DISPATCH_INTERVAL_SECONDS)


async def _timeout_loop() -> None:
    while True:
        now = time.time()
        async with _lock:
            for rec in _tasks.values():
                if rec.status != "processing" or rec.assigned_at is None:
                    continue
                if now - rec.assigned_at < TASK_TIMEOUT_SECONDS:
                    continue

                rec.retries += 1
                rec.assigned_at = None
                rec.worker_id = None
                if rec.retries > MAX_RETRIES:
                    rec.status = "failed"
                    rec.error = "task timed out after retries"
                else:
                    rec.status = "pending"
                    _pending.append(rec.task_id)
        await asyncio.sleep(1.0)
