"""
main.py — FastAPI server for PRIYA.

REST endpoints, WebSocket streaming, internal event/approval plumbing.
Run: uvicorn main:app --host 0.0.0.0 --port 8000 --reload
"""
import asyncio
import csv
import io
import json
import os
import pathlib
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from agent import PriyaAgentSession
from db import PriyaDB

load_dotenv()

UPLOAD_DIR = pathlib.Path(__file__).parent / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
db = PriyaDB()
active_sessions: dict[str, PriyaAgentSession] = {}
active_connections: dict[str, list[WebSocket]] = {}  # run_id -> [ws]

# Blocking gates: (type, id) -> Future that resolves when approved
_wait_gates: dict[tuple[str, str], asyncio.Future] = {}
# Pre-approved gates: store results for approvals that arrive before the wait
_pre_approved: dict[tuple[str, str], dict] = {}


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init()
    yield
    await db.close()


app = FastAPI(title="PRIYA", version="1.0.0", lifespan=lifespan)

import traceback as _tb
from starlette.requests import Request as StarletteRequest
from starlette.responses import JSONResponse as StarletteJSONResponse

@app.exception_handler(Exception)
async def _unhandled(request: StarletteRequest, exc: Exception):
    _tb.print_exc()
    return StarletteJSONResponse({"error": str(exc), "type": type(exc).__name__}, status_code=500)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# WebSocket helpers
# ---------------------------------------------------------------------------
async def ws_broadcast(run_id: str, event: dict) -> None:
    """Send an event to all WebSocket clients for a run."""
    event.setdefault("run_id", run_id)
    event.setdefault("ts", datetime.utcnow().isoformat())
    payload = json.dumps(event)
    conns = active_connections.get(run_id, [])
    dead: list[WebSocket] = []
    for ws in conns:
        try:
            await ws.send_text(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        conns.remove(ws)


# ---------------------------------------------------------------------------
# Request/response models
# ---------------------------------------------------------------------------
class RunRequest(BaseModel):
    persona: str
    instruction: str
    csv_file: Optional[str] = None  # path to uploaded CSV


class QueryRequest(BaseModel):
    question: str


class EscalationRequest(BaseModel):
    decision: str  # "approve" | "reject" | "override"
    reason: Optional[str] = None


class InternalEvent(BaseModel):
    event_type: Optional[str] = None
    payload: Optional[dict] = None
    event: Optional[dict] = None


class InternalApproval(BaseModel):
    result: dict


# ---------------------------------------------------------------------------
# REST — Run lifecycle
# ---------------------------------------------------------------------------
@app.post("/run")
async def start_run(req: RunRequest):
    run_id = f"run-{uuid.uuid4().hex[:12]}"

    if not req.csv_file:
        raise HTTPException(400, "csv_file path is required")

    csv_path = req.csv_file
    if not pathlib.Path(csv_path).exists():
        raise HTTPException(400, f"CSV file not found: {csv_path}")

    # Count vendors and total from CSV
    with open(csv_path, newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    total_vendors = len(rows)
    total_amount = sum(float(r.get("amount", 0)) for r in rows)

    # Create DB run record
    now = datetime.utcnow().isoformat()
    await db.create_run({
        "id": run_id,
        "persona": req.persona,
        "instruction": req.instruction,
        "invoice_source": csv_path,
        "pine_token": "",  # agent will obtain via MCP
        "status": "running",
        "total_vendors": total_vendors,
        "total_amount": total_amount,
        "paid_amount": 0,
        "deferred_amount": 0,
        "float_saved": 0,
        "started_at": now,
        "completed_at": None,
        "approved_at": None,
    })

    # Create agent session and run in background
    session = PriyaAgentSession(
        run_id=run_id,
        persona=req.persona,
        instruction=req.instruction,
        csv_path=csv_path,
    )
    active_sessions[run_id] = session

    async def _run_agent():
        try:
            await session.execute(
                ws_broadcast=lambda evt: ws_broadcast(run_id, evt),
            )
            await db.update_run_status(run_id, "completed")
            await ws_broadcast(run_id, {"type": "RUN_COMPLETE"})
        except Exception as e:
            await db.update_run_status(run_id, "failed")
            await ws_broadcast(run_id, {"type": "RUN_FAILED", "error": str(e)})

    asyncio.create_task(_run_agent())

    return {"run_id": run_id, "status": "started", "total_vendors": total_vendors, "total_amount": total_amount}


@app.post("/approve/{run_id}")
async def approve_run(run_id: str):
    resolved = False
    for gate_type in ("approve", "approval"):
        key = (gate_type, run_id)
        if key in _wait_gates and not _wait_gates[key].done():
            _wait_gates[key].set_result({"approved": True})
            resolved = True
    # If gate doesn't exist yet, store as pre-approved
    if not resolved:
        for gate_type in ("approve", "approval"):
            _pre_approved[(gate_type, run_id)] = {"approved": True}
    await db.update_run_status(run_id, "approved", approved_at=datetime.utcnow().isoformat())
    await ws_broadcast(run_id, {"type": "APPROVAL_GRANTED"})
    return {"approved": True}


@app.post("/escalation/{run_id}/{vendor_id}")
async def handle_escalation(run_id: str, vendor_id: str, req: EscalationRequest):
    key = ("escalation", f"{run_id}:{vendor_id}")
    if key in _wait_gates and not _wait_gates[key].done():
        _wait_gates[key].set_result({"decision": req.decision, "reason": req.reason})
    await ws_broadcast(run_id, {
        "type": "ESCALATION_RESOLVED",
        "vendor_id": vendor_id,
        "decision": req.decision,
    })
    return {"ok": True}


@app.post("/query/{run_id}")
async def query_run(run_id: str, req: QueryRequest):
    session = active_sessions.get(run_id)
    if not session:
        raise HTTPException(404, f"No active session for run {run_id}")
    query_id = await session.send_nl_query(
        question=req.question,
        ws_broadcast=lambda evt: ws_broadcast(run_id, evt),
    )
    return {"query_id": query_id}


@app.get("/runs")
async def list_runs():
    rows = await db._fetchall("SELECT * FROM runs ORDER BY started_at DESC")
    return {"runs": rows}


@app.get("/run/{run_id}")
async def get_run(run_id: str):
    run = await db.get_run(run_id)
    if not run:
        raise HTTPException(404, "Run not found")
    orders = await db.get_orders_by_run(run_id)
    payments = []
    for order in orders:
        payments.extend(await db.get_payments_by_order(order["id"]))
    settlements = await db.get_settlements_by_run(run_id)
    recons = await db.get_reconciliations_by_run(run_id)
    return {
        "run": run,
        "orders": orders,
        "payments": payments,
        "settlements": settlements,
        "reconciliations": recons,
    }


@app.get("/run/{run_id}/export")
async def export_run(run_id: str):
    recons = await db.get_reconciliations_by_run(run_id)
    if not recons:
        raise HTTPException(404, "No reconciliation data for this run")

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=recons[0].keys())
    writer.writeheader()
    writer.writerows(recons)
    output.seek(0)

    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=priya_run_{run_id}.csv"},
    )


@app.post("/upload-csv")
async def upload_csv(file: UploadFile):
    if not file.filename or not file.filename.endswith(".csv"):
        raise HTTPException(400, "Only CSV files are accepted")

    content = await file.read()
    dest = UPLOAD_DIR / f"{uuid.uuid4().hex[:8]}_{file.filename}"
    dest.write_bytes(content)

    # Count vendors
    reader = csv.DictReader(io.StringIO(content.decode("utf-8")))
    rows = list(reader)

    return {"file_path": str(dest), "vendor_count": len(rows)}


# ---------------------------------------------------------------------------
# Internal endpoints — called by MCP server
# ---------------------------------------------------------------------------
@app.post("/internal/event")
async def internal_event(req: InternalEvent):
    """Broadcast a WebSocket event from the MCP server."""
    # Support both formats: {event: {...}} and {event_type: ..., payload: {...}}
    if req.event:
        event = req.event
    elif req.event_type and req.payload:
        event = {"type": req.event_type, **req.payload}
    else:
        return {"ok": False, "error": "no event data"}
    run_id = event.get("run_id", "")
    await ws_broadcast(run_id, event)
    return {"ok": True}


@app.post("/internal/wait/{gate_type}/{gate_id}")
async def internal_wait(gate_type: str, gate_id: str):
    """Block until an approval/escalation decision is made. Called by MCP server."""
    key = (gate_type, gate_id)
    # Check if already pre-approved
    if key in _pre_approved:
        result = _pre_approved.pop(key)
        return result

    loop = asyncio.get_event_loop()
    future = loop.create_future()
    _wait_gates[key] = future

    try:
        result = await asyncio.wait_for(future, timeout=600)  # 10 min timeout
    except asyncio.TimeoutError:
        _wait_gates.pop(key, None)
        raise HTTPException(408, f"Timeout waiting for {gate_type}/{gate_id}")
    finally:
        _wait_gates.pop(key, None)

    return result


@app.post("/internal/approve/{gate_type}/{gate_id}")
async def internal_approve(gate_type: str, gate_id: str, req: InternalApproval):
    """Unblock a waiting gate. Called by frontend via REST."""
    key = (gate_type, gate_id)
    if key in _wait_gates and not _wait_gates[key].done():
        _wait_gates[key].set_result(req.result)
        return {"ok": True}
    raise HTTPException(404, f"No waiting gate for {gate_type}/{gate_id}")


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------
@app.websocket("/ws/{run_id}")
async def websocket_endpoint(websocket: WebSocket, run_id: str):
    await websocket.accept()
    if run_id not in active_connections:
        active_connections[run_id] = []
    active_connections[run_id].append(websocket)

    try:
        while True:
            # Keep connection alive; client can send pings or commands
            data = await websocket.receive_text()
            # Handle client messages if needed (e.g., approval from WS)
            try:
                msg = json.loads(data)
                if msg.get("type") == "approve":
                    for gate_type in ("approve", "approval"):
                        key = (gate_type, run_id)
                        if key in _wait_gates and not _wait_gates[key].done():
                            _wait_gates[key].set_result({"approved": True})
            except json.JSONDecodeError:
                pass
    except WebSocketDisconnect:
        pass
    finally:
        conns = active_connections.get(run_id, [])
        if websocket in conns:
            conns.remove(websocket)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
