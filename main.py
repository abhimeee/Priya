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
from fastapi import FastAPI, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from agent import PriyaAgentSession
from db import PriyaDB
from pine_client import PineLabs

load_dotenv()

# Allow Claude Agent SDK to run as a subprocess even when launched from Claude Code
os.environ.pop("CLAUDECODE", None)

UPLOAD_DIR = pathlib.Path(__file__).parent / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
db = PriyaDB()
pine = PineLabs()
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
    allow_origins=["http://localhost:3000", "http://localhost:4173", "http://localhost:5173"],
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
    event.setdefault("timestamp", datetime.utcnow().isoformat())
    # Ensure payload wrapper for frontend compatibility
    if "payload" not in event:
        payload = {k: v for k, v in event.items() if k not in ("type", "run_id", "timestamp")}
        event = {"type": event.get("type", ""), "payload": payload, "run_id": event.get("run_id", run_id), "timestamp": event.get("timestamp", "")}
    text = json.dumps(event)
    conns = active_connections.get(run_id, [])
    dead: list[WebSocket] = []
    for ws in conns:
        try:
            await ws.send_text(text)
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
async def start_run(request: Request):
    content_type = request.headers.get("content-type", "")

    if "multipart/form-data" in content_type:
        form = await request.form()
        persona = form.get("persona", "hospital")
        instruction = form.get("instruction", "")
        csv_file_field = form.get("csv_file")

        if csv_file_field and hasattr(csv_file_field, 'read'):
            content = await csv_file_field.read()
            filename = getattr(csv_file_field, 'filename', 'upload.csv')
            dest = UPLOAD_DIR / f"{uuid.uuid4().hex[:8]}_{filename}"
            dest.write_bytes(content)
            csv_path = str(dest)
        elif isinstance(csv_file_field, str):
            csv_path = csv_file_field
        else:
            raise HTTPException(400, "csv_file is required")
    else:
        body = await request.json()
        persona = body.get("persona", "hospital")
        instruction = body.get("instruction", "")
        csv_path = body.get("csv_file", "")

    if not csv_path or not pathlib.Path(csv_path).exists():
        raise HTTPException(400, f"CSV file not found: {csv_path}")

    run_id = f"run-{uuid.uuid4().hex[:12]}"

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
        "persona": persona,
        "instruction": instruction,
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
        persona=persona,
        instruction=instruction,
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
    # Map frontend values to backend values
    decision = req.decision
    if decision == "capture":
        decision = "approve"
    elif decision == "cancel":
        decision = "reject"

    key = ("escalation", f"{run_id}:{vendor_id}")
    if key in _wait_gates and not _wait_gates[key].done():
        _wait_gates[key].set_result({"decision": decision, "reason": req.reason})
    await ws_broadcast(run_id, {
        "type": "ESCALATION_RESOLVED",
        "vendor_id": vendor_id,
        "decision": decision,
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


@app.get("/invoices/{persona}")
async def get_invoices(persona: str):
    """Return invoice CSV data for a persona as JSON (for frontend sync)."""
    csv_path = pathlib.Path(__file__).parent / "invoices" / f"{persona}_invoices.csv"
    if not csv_path.exists():
        raise HTTPException(404, f"No invoice file for persona: {persona}")
    with open(csv_path, newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    return {"invoices": rows, "count": len(rows), "file_path": str(csv_path)}


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
# REST — Reconciliation tab endpoints
# ---------------------------------------------------------------------------

def _parse_checks(recon: dict) -> list[dict]:
    """Parse the JSON checks field into a list; return [] on failure."""
    raw = recon.get("checks")
    if not raw:
        return []
    try:
        return json.loads(raw)
    except Exception:
        return []


def _recon_with_parsed_checks(recon: dict) -> dict:
    """Return a copy of recon dict with checks parsed from JSON string to list."""
    r = dict(recon)
    r["checks"] = _parse_checks(recon)
    return r


@app.get("/api/reconciliation/{run_id}")
async def get_reconciliation(run_id: str):
    """Get all reconciliation data for a run with computed checks."""
    run = await db.get_run(run_id)
    if not run:
        raise HTTPException(404, "Run not found")

    recons_raw = await db.get_reconciliations_by_run(run_id)
    recons = [_recon_with_parsed_checks(r) for r in recons_raw]

    total = len(recons)
    matched = sum(1 for r in recons if r.get("recon_status") == "MATCHED")
    mismatch = sum(1 for r in recons if r.get("recon_status") == "MISMATCH")
    warning = sum(1 for r in recons if r.get("recon_status") == "WARNING")
    pending = sum(1 for r in recons if r.get("recon_status") == "PENDING")

    all_checks = []
    for r in recons:
        all_checks.extend(r.get("checks") or [])

    checks_summary = {
        "blocking_failures": sum(
            1 for c in all_checks if c.get("severity") == "blocking" and not c.get("passed")
        ),
        "warnings": sum(
            1 for c in all_checks if c.get("severity") == "warning" and not c.get("passed")
        ),
        "info_flags": sum(
            1 for c in all_checks if c.get("severity") == "info" and not c.get("passed")
        ),
    }

    summary = {
        "total": total,
        "matched": matched,
        "mismatch": mismatch,
        "warning": warning,
        "pending": pending,
        "match_rate": round(matched / total * 100, 2) if total else 0.0,
    }

    return {"reconciliations": recons, "summary": summary, "checks_summary": checks_summary}


@app.get("/api/reconciliation/{run_id}/scorecard")
async def get_recon_scorecard(run_id: str):
    """Get the scorecard metrics for the reconciliation tab."""
    run = await db.get_run(run_id)
    if not run:
        raise HTTPException(404, "Run not found")

    settlements = await db.get_settlements_by_run(run_id)
    recons_raw = await db.get_reconciliations_by_run(run_id)
    recons = [_recon_with_parsed_checks(r) for r in recons_raw]

    total_settlements = len(settlements)
    total_amount = round(sum(s.get("settled_amount", 0.0) or 0.0 for s in settlements), 2)

    total_recons = len(recons)
    matched_count = sum(1 for r in recons if r.get("recon_status") == "MATCHED")
    mismatch_count = sum(1 for r in recons if r.get("recon_status") == "MISMATCH")
    match_rate = round(matched_count / total_recons * 100, 2) if total_recons else 0.0

    mdr_drift_count = sum(1 for r in recons if r.get("mdr_drift_flagged"))
    delayed_count = sum(
        1 for r in recons if (r.get("settlement_delay_days") or 0) > 2
    )
    pending_bank_count = sum(
        1 for r in recons if r.get("bank_credit_amount") is None
    )
    refund_pending = round(
        sum(s.get("refund_debit", 0.0) or 0.0 for s in settlements), 2
    )

    return {
        "total_settlements": total_settlements,
        "total_amount": total_amount,
        "match_rate": match_rate,
        "mismatch_count": mismatch_count,
        "mdr_drift_count": mdr_drift_count,
        "delayed_count": delayed_count,
        "pending_bank_count": pending_bank_count,
        "refund_pending": refund_pending,
    }


@app.get("/api/reconciliation/{run_id}/utr/{utr_number}")
async def get_utr_detail(run_id: str, utr_number: str):
    """Get detailed UTR drilldown: all settlements + associated orders + MDR breakdown."""
    run = await db.get_run(run_id)
    if not run:
        raise HTTPException(404, "Run not found")

    # All settlements with this UTR (may be >1 if duplicate UTR issue)
    settlements = await db.get_settlements_by_utr(utr_number)
    if not settlements:
        raise HTTPException(404, f"No settlements found for UTR {utr_number}")

    # Reconciliation records for this run + UTR
    recons_raw = await db.get_reconciliations_by_utr(run_id, utr_number)
    recons = [_recon_with_parsed_checks(r) for r in recons_raw]

    # Fetch associated orders
    pine_order_ids = {s["pine_order_id"] for s in settlements if s.get("pine_order_id")}
    all_orders = await db.get_orders_by_run(run_id)
    related_orders = [o for o in all_orders if o.get("pine_order_id") in pine_order_ids]

    # MDR breakdown (aggregate across settlements)
    total_gross = round(sum(s.get("expected_amount", 0.0) or 0.0 for s in settlements), 2)
    total_net = round(sum(s.get("settled_amount", 0.0) or 0.0 for s in settlements), 2)
    total_mdr = round(sum(s.get("platform_fee", 0.0) or 0.0 for s in settlements), 2)
    total_refund_debit = round(sum(s.get("refund_debit", 0.0) or 0.0 for s in settlements), 2)
    total_deductions = round(sum(s.get("total_deduction_amount", 0.0) or 0.0 for s in settlements), 2)
    effective_mdr_rate = round(total_mdr / total_gross, 6) if total_gross else 0.0

    mdr_breakdown = {
        "total_gross_amount": total_gross,
        "total_net_amount": total_net,
        "total_platform_fee": total_mdr,
        "total_refund_debit": total_refund_debit,
        "total_deductions": total_deductions,
        "effective_mdr_rate": effective_mdr_rate,
    }

    return {
        "utr_number": utr_number,
        "settlements": settlements,
        "reconciliations": recons,
        "orders": related_orders,
        "mdr_breakdown": mdr_breakdown,
        "duplicate_utr": len(settlements) > 1,
    }


@app.post("/api/reconciliation/{run_id}/bank-credit")
async def update_bank_credit(run_id: str, data: dict):
    """Update bank_credit_amount for a specific UTR/settlement and re-run check 2."""
    utr_number = data.get("utr_number")
    bank_credit_amount = data.get("bank_credit_amount")

    if not utr_number:
        raise HTTPException(400, "utr_number is required")
    if bank_credit_amount is None:
        raise HTTPException(400, "bank_credit_amount is required")

    run = await db.get_run(run_id)
    if not run:
        raise HTTPException(404, "Run not found")

    recons_raw = await db.get_reconciliations_by_utr(run_id, utr_number)
    if not recons_raw:
        raise HTTPException(404, f"No reconciliation records for UTR {utr_number} in run {run_id}")

    updated = []
    for recon in recons_raw:
        settled_amount = recon.get("settled_amount", 0.0) or 0.0
        bank_delta = round(bank_credit_amount - settled_amount, 2)

        # Re-evaluate checks with new bank_credit_amount
        existing_checks = _parse_checks(recon)
        # Update check 2 in place
        new_checks = []
        for c in existing_checks:
            if c.get("check_id") == 2:
                c2_passed = abs(bank_delta) < 0.01
                new_checks.append({
                    "check_id": 2,
                    "name": "bank_credit_match",
                    "severity": "blocking",
                    "passed": c2_passed,
                    "detail": (
                        f"bank_credit={bank_credit_amount}, settled={settled_amount}, "
                        f"delta={bank_delta}"
                    ),
                })
            else:
                new_checks.append(c)

        # If no checks yet, create a minimal check 2
        if not new_checks:
            c2_passed = abs(bank_delta) < 0.01
            new_checks = [{
                "check_id": 2,
                "name": "bank_credit_match",
                "severity": "blocking",
                "passed": c2_passed,
                "detail": f"bank_credit={bank_credit_amount}, settled={settled_amount}, delta={bank_delta}",
            }]

        # Recompute recon_status
        blocking_failed = any(
            c for c in new_checks
            if c["severity"] == "blocking" and c.get("passed") is False
        )
        warning_failed = any(
            c for c in new_checks
            if c["severity"] == "warning" and c.get("passed") is False
        )
        if blocking_failed:
            recon_status = "MISMATCH"
        elif warning_failed:
            recon_status = "WARNING"
        else:
            recon_status = "MATCHED"

        checks_json = json.dumps(new_checks)
        await db.update_bank_credit(
            recon_id=recon["id"],
            bank_credit_amount=bank_credit_amount,
            bank_delta=bank_delta,
            recon_status=recon_status,
            checks=checks_json,
        )
        updated.append({
            "recon_id": recon["id"],
            "utr_number": utr_number,
            "bank_credit_amount": bank_credit_amount,
            "bank_delta": bank_delta,
            "recon_status": recon_status,
            "checks": new_checks,
        })

    return {"updated": updated, "count": len(updated)}


@app.get("/api/settlements/live")
async def get_live_settlements(start_date: str = None, end_date: str = None):
    """Fetch settlements directly from Pine Labs API for standalone reconciliation."""
    if not start_date:
        raise HTTPException(400, "start_date is required (YYYY-MM-DD)")
    if not end_date:
        raise HTTPException(400, "end_date is required (YYYY-MM-DD)")

    # Get a fresh token
    try:
        token_resp = await pine.generate_token()
        token = token_resp.get("access_token") or token_resp.get("token", "")
    except Exception as e:
        raise HTTPException(502, f"Failed to obtain Pine Labs token: {e}")

    all_settlements: list[dict] = []
    page = 1
    per_page = 50

    while True:
        try:
            result = await pine.get_all_settlements(
                token, start_date, end_date, page=page, per_page=per_page
            )
        except Exception as e:
            raise HTTPException(502, f"Pine Labs settlements API error: {e}")

        batch = result.get("data", [])
        all_settlements.extend(batch)

        # Pagination: stop if fewer results than requested
        total = result.get("total", len(all_settlements))
        if len(batch) < per_page or len(all_settlements) >= total:
            break
        page += 1

    return {
        "settlements": all_settlements,
        "count": len(all_settlements),
        "start_date": start_date,
        "end_date": end_date,
    }


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
        event = {"type": req.event_type, "payload": req.payload}
    else:
        return {"ok": False, "error": "no event data"}
    run_id = event.get("run_id", "") or (event.get("payload", {}).get("run_id", ""))
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
# Pine Labs Webhook receiver
# ---------------------------------------------------------------------------
import logging

_wh_log = logging.getLogger("priya.webhook")

# Map Pine Labs webhook event_type to our internal status
_WEBHOOK_STATUS_MAP = {
    "ORDER_PROCESSED": "PROCESSED",
    "ORDER_AUTHORIZED": "AUTHORIZED",
    "ORDER_CANCELLED": "CANCELLED",
    "ORDER_FAILED": "FAILED",
    "PAYMENT_FAILED": "FAILED",
    "REFUND_PROCESSED": "REFUND_PROCESSED",
    "REFUND_FAILED": "REFUND_FAILED",
}


@app.post("/webhook/pinelabs")
async def pinelabs_webhook(request: Request):
    """
    Receive webhook callbacks from Pine Labs (Plural).
    Updates order + payment status in DB and broadcasts WS events.
    Docs: https://docs.pinelabs.com/docs/webhooks
    """
    body = await request.json()
    event_type = body.get("event_type", "")
    data = body.get("data", {})
    pine_order_id = data.get("order_id", "")

    _wh_log.info("Webhook received: %s for order %s", event_type, pine_order_id)

    if not pine_order_id:
        return {"ok": False, "error": "no order_id in webhook payload"}

    # Look up our internal order
    order = await db.get_order_by_pine_id(pine_order_id)
    if not order:
        _wh_log.warning("Webhook for unknown order: %s", pine_order_id)
        return {"ok": False, "error": f"order {pine_order_id} not found in PRIYA DB"}

    run_id = order.get("run_id", "")
    order_status = data.get("status", "")

    # Update order status
    new_status = _WEBHOOK_STATUS_MAP.get(event_type, order_status)
    if new_status:
        await db.update_order_status(order["id"], new_status)

    # Update individual payment statuses from the webhook payload
    wh_payments = data.get("payments", [])
    db_payments = await db.get_payments_by_pine_order(pine_order_id)

    for wh_pay in wh_payments:
        wh_pay_id = wh_pay.get("id", "")
        wh_pay_status = wh_pay.get("status", "")
        wh_pay_amount = wh_pay.get("payment_amount", {}).get("value")

        # Match by pine_payment_id
        matched = next(
            (p for p in db_payments if p.get("pine_payment_id") == wh_pay_id), None
        )
        # Fallback: match by merchant_payment_reference
        if not matched:
            wh_ref = wh_pay.get("merchant_payment_reference", "")
            matched = next(
                (p for p in db_payments if p.get("merchant_payment_reference") == wh_ref),
                None,
            )
        # Fallback: if only one payment, use it
        if not matched and len(db_payments) == 1:
            matched = db_payments[0]

        if matched and wh_pay_status:
            extra = {}
            # Store acquirer data (RRN, approval code)
            acq = wh_pay.get("acquirer_data", {})
            if acq.get("rrn"):
                extra["webhook_event"] = json.dumps({
                    "rrn": acq.get("rrn"),
                    "approval_code": acq.get("approval_code"),
                    "acquirer_reference": acq.get("acquirer_reference"),
                })
            if wh_pay_amount is not None:
                extra["amount"] = float(wh_pay_amount)
            # Error detail for failures
            err = wh_pay.get("error_detail")
            if err:
                extra["failure_reason"] = f"{err.get('code', '')}: {err.get('message', '')}"

            await db.update_payment_status(matched["id"], wh_pay_status, **extra)

    # Broadcast to frontend via WebSocket
    await ws_broadcast(run_id, {
        "type": "PIPELINE_STEP",
        "payload": {
            "run_id": run_id,
            "step": f"webhook_{event_type.lower()}",
            "status": new_status,
            "pine_order_id": pine_order_id,
            "vendor_id": order.get("vendor_id", ""),
            "detail": f"Pine Labs webhook: {event_type}",
        },
    })

    _wh_log.info("Webhook processed: %s -> status %s", pine_order_id, new_status)
    return {"ok": True, "event_type": event_type, "order_id": pine_order_id, "status": new_status}


@app.post("/api/confirm-payment/{run_id}")
async def manual_confirm_payment(run_id: str):
    """
    Manual payment confirmation for demo/hackathon.
    Simulates the ORDER_PROCESSED webhook by updating all AWAITING_PAYMENT
    payments in this run to PROCESSED. Use when Pine Labs UAT webhook
    isn't configured to reach our server.
    """
    orders = await db.get_orders_by_run(run_id)
    if not orders:
        raise HTTPException(404, "No orders for this run")

    confirmed = 0
    vendor_states = []
    for order in orders:
        payments = await db.get_payments_by_order(order["id"])
        for p in payments:
            if p.get("pine_status") in ("AWAITING_PAYMENT", "PENDING"):
                await db.update_payment_status(p["id"], "PROCESSED")
                confirmed += 1
        # Also update order status
        if order.get("pine_status") in ("AWAITING_PAYMENT", "PENDING", "CREATED"):
            await db.update_order_status(order["id"], "PROCESSED")
            vendor_states.append({
                "vendor_id": order.get("vendor_id", ""),
                "name": order.get("vendor_id", ""),
                "amount": order.get("amount", 0),
                "rail": "payment_link",
                "state": "PROCESSED",
                "pine_order_id": order.get("pine_order_id", ""),
            })

    # Broadcast vendor state update so frontend shows PROCESSED
    if vendor_states:
        await ws_broadcast(run_id, {
            "type": "VENDOR_STATE",
            "payload": {
                "run_id": run_id,
                "vendors": vendor_states,
            },
        })

    # Broadcast pipeline step
    await ws_broadcast(run_id, {
        "type": "PIPELINE_STEP",
        "payload": {
            "run_id": run_id,
            "step": "webhook_order_processed",
            "status": "PROCESSED",
            "detail": f"Manual confirmation: {confirmed} payment(s) confirmed",
        },
    })

    return {"ok": True, "confirmed_count": confirmed}


# ---------------------------------------------------------------------------
# Standalone NL → SQL → Chart query (works without active agent session)
# ---------------------------------------------------------------------------

_NL2SQL_SCHEMA = """
Tables:
  vendors(id, name, persona, category, upi_id, bank_account, ifsc, preferred_rail, credit_days, vendor_type, drug_schedule, is_compliant, amount, invoice_number, invoice_date, due_date, items, priority_score, priority_reason, action, created_at)
  runs(id, persona, instruction, invoice_source, pine_token, status, total_vendors, total_amount, paid_amount, deferred_amount, float_saved, started_at, completed_at, approved_at)
  orders(id, run_id, vendor_id, pine_order_id, merchant_order_reference, amount, priority_score, priority_reason, action, pre_auth, pine_status, escalation_flag, defer_reason, created_at, updated_at)
  payments(id, run_id, order_id, vendor_id, pine_order_id, pine_payment_id, merchant_payment_reference, amount, rail, attempt_number, pine_status, failure_reason, recovery_action, webhook_event, request_id, initiated_at, confirmed_at)
  settlements(id, run_id, pine_order_id, pine_settlement_id, utr_number, bank_account, last_processed_date, expected_amount, settled_amount, platform_fee, total_deduction_amount, refund_debit, fee_flagged, status, settled_at, created_at)
  reconciliations(id, run_id, order_id, payment_id, settlement_id, vendor_id, pine_order_id, merchant_order_reference, utr_number, persona, invoice_amount, paid_amount, settled_amount, variance, mdr_rate_actual, mdr_rate_contracted, mdr_drift_flagged, rail_used, retries, outcome, pre_auth_used, agent_reasoning, ca_notes, created_at)

Key relationships:
  order_id = Pine Labs spine (links orders->payments->settlements)
  merchant_order_reference = PRIYA spine (links our records)
  utr_number = reconciliation anchor (links settlements->reconciliations)
"""

_NL2SQL_SYSTEM = f"""You are a SQL assistant for the PRIYA payment platform. Given a natural language question, generate a SQLite SELECT query.

{_NL2SQL_SCHEMA}

Rules:
- Output ONLY valid JSON with keys: "sql" (the SELECT query), "render_as" (one of: "summary_card", "bar_chart", "pie_chart", "line_chart", "table")
- render_as guidelines:
  - "summary_card": single aggregate value (COUNT, SUM, AVG, MAX, MIN) returning 1 row / 1-2 columns
  - "bar_chart": grouped aggregation with a label column + ONE numeric column, 2-15 categories (e.g. SUM(amount) GROUP BY vendor_name)
  - "pie_chart": like bar_chart but <=8 categories showing proportions (e.g. distribution by status)
  - "line_chart": time-series data with a date/time column + numeric columns
  - "table": detail listings, multiple columns per row, or anything that doesn't fit above. THIS IS THE DEFAULT — use table when unsure.
- IMPORTANT: if the user asks to "show" or "list" rows/records/orders/payments, use "table" — NOT bar_chart
- IMPORTANT: bar_chart requires exactly ONE label column and ONE numeric aggregate column from a GROUP BY. Do NOT use bar_chart for detail rows with many columns.
- For detail queries, select only the most useful columns (name, amount, status, dates) — not every column in the table
- Use readable aliases (e.g. AS vendor_name, AS total_amount)
- All monetary values are in Rupees
- If a run_id filter is needed, use the provided run_id
- Only generate SELECT queries — never INSERT, UPDATE, DELETE
- Keep queries efficient — use LIMIT 50 for large result sets
- No markdown, no explanation — just the JSON object
"""


class ChatQueryRequest(BaseModel):
    question: str
    run_id: Optional[str] = None


def _get_anthropic_client():
    """Create Anthropic client (Bedrock or direct API)."""
    use_bedrock = os.getenv("CLAUDE_CODE_USE_BEDROCK", "0") == "1"
    if use_bedrock:
        from anthropic import AnthropicBedrock
        return AnthropicBedrock(
            aws_region=os.getenv("AWS_REGION", "us-east-1"),
        )
    else:
        from anthropic import Anthropic
        return Anthropic()


@app.post("/api/chat-query")
async def standalone_chat_query(req: ChatQueryRequest):
    """
    NL → SQL → Chart: standalone query endpoint that works without an active agent.
    Uses Claude API directly to convert natural language to SQL, executes it,
    and returns structured data for DynamicQueryView rendering.
    """
    question = req.question.strip()
    if not question:
        raise HTTPException(400, "question is required")

    # Build the user prompt with optional run_id context
    user_prompt = question
    if req.run_id:
        user_prompt = f"Run ID: {req.run_id}\n\nQuestion: {question}"

    # Call Claude API to generate SQL
    try:
        client = _get_anthropic_client()
        model = os.getenv("ANTHROPIC_DEFAULT_SONNET_MODEL", "us.anthropic.claude-sonnet-4-6")

        # Run in thread pool since anthropic client is sync
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(None, lambda: client.messages.create(
            model=model,
            max_tokens=512,
            system=_NL2SQL_SYSTEM,
            messages=[{"role": "user", "content": user_prompt}],
        ))

        raw_text = response.content[0].text.strip()
        # Parse JSON (handle potential markdown wrapping)
        if raw_text.startswith("```"):
            raw_text = raw_text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        parsed = json.loads(raw_text)
        sql = parsed["sql"]
        render_as = parsed.get("render_as", "table")

    except json.JSONDecodeError:
        raise HTTPException(502, f"Claude returned invalid JSON: {raw_text[:200]}")
    except Exception as e:
        raise HTTPException(502, f"Claude API error: {e}")

    # Execute the SQL query
    try:
        rows = await db.execute_query(sql)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"SQL execution error: {e}")

    columns = list(rows[0].keys()) if rows else []
    # Convert rows to list-of-lists for DynamicQueryView
    rows_array = [[row[col] for col in columns] for row in rows]

    result = {
        "query_nl": question,
        "sql": sql,
        "columns": columns,
        "rows": rows_array,
        "row_count": len(rows_array),
        "render_as": render_as,
    }

    # Broadcast via WebSocket if run_id provided
    if req.run_id:
        await ws_broadcast(req.run_id, {
            "type": "QUERY_RESULT",
            "payload": result,
        })

    return result


# ---------------------------------------------------------------------------
# Policy YAML endpoints
# ---------------------------------------------------------------------------
PERSONAS_DIR = pathlib.Path(__file__).parent / "personas"

@app.get("/api/policy/{persona}")
async def get_policy(persona: str):
    """Return the persona policy YAML as JSON."""
    import yaml
    path = PERSONAS_DIR / f"{persona}.yaml"
    if not path.exists():
        raise HTTPException(404, f"Persona '{persona}' not found")
    with open(path) as f:
        return yaml.safe_load(f)


@app.put("/api/policy/{persona}")
async def update_policy(persona: str, request: Request):
    """Update the persona policy YAML from JSON body."""
    import yaml
    path = PERSONAS_DIR / f"{persona}.yaml"
    if not path.exists():
        raise HTTPException(404, f"Persona '{persona}' not found")
    body = await request.json()
    with open(path, "w") as f:
        yaml.dump(body, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
    return {"ok": True, "persona": persona}


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
