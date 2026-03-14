"""
PRIYA custom MCP server — 17 domain tools exposed over stdio JSON-RPC.

All monetary values flowing through MCP tools are in RUPEES (float).
Conversion to PAISA (int) happens at the pine_client boundary.

WS events are emitted via HTTP POST to http://localhost:8000/internal/event.
Blocking tools POST to http://localhost:8000/internal/wait/{type}/{id}
and wait until approval arrives.

Run as subprocess via the agent SDK (stdio transport).
"""
from __future__ import annotations

import csv
import json
import os
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx
import yaml
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from db import PriyaDB
from pine_client import PineLabs
from scorer import score_vendors as _score_vendors, compute_credit_float

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
FASTAPI_INTERNAL = os.getenv("FASTAPI_INTERNAL_URL", "http://localhost:8000/internal")
RUN_ID = os.getenv("PRIYA_RUN_ID", "")
DB_PATH = os.getenv("DB_PATH", "./priya.db")
PERSONAS_DIR = os.getenv("PERSONAS_DIR", "./personas")
INVOICES_DIR = os.getenv("INVOICES_DIR", "./invoices")
WAIT_TIMEOUT = float(os.getenv("PRIYA_WAIT_TIMEOUT_S", "300"))

# Rail fallback chains (from persona yamls, also hardcoded here as defaults)
RAIL_FALLBACKS: dict[str, list[str]] = {
    "neft": ["upi_intent", "hosted_checkout", "payment_link"],
    "upi": ["upi_intent", "hosted_checkout", "payment_link"],
    "upi_collect": ["upi_intent", "hosted_checkout", "payment_link"],
    "upi_intent": ["hosted_checkout", "payment_link"],
    "netbanking": ["upi_intent", "hosted_checkout", "payment_link"],
    "hosted_checkout": ["payment_link"],
    "payment_link": [],
}

# ---------------------------------------------------------------------------
# Singletons
# ---------------------------------------------------------------------------
_db = PriyaDB(DB_PATH)
_pine = PineLabs()
app = Server("priya-mcp-server")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _rupees_to_paisa(rupees: float) -> int:
    return int(round(rupees * 100))


def _load_persona(persona: str) -> dict:
    path = os.path.join(PERSONAS_DIR, f"{persona}.yaml")
    with open(path) as f:
        return yaml.safe_load(f)


async def _emit_event(event_type: str, payload: dict) -> None:
    """Fire-and-forget WS event via FastAPI internal endpoint."""
    try:
        payload.setdefault("run_id", RUN_ID)
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(
                f"{FASTAPI_INTERNAL}/event",
                json={"event_type": event_type, "payload": payload},
            )
    except Exception:
        pass  # MCP server must not crash if FastAPI is not yet up


async def _wait_for_approval(wait_type: str, wait_id: str) -> dict:
    """Block until FastAPI signals approval/decision for this id."""
    async with httpx.AsyncClient(timeout=WAIT_TIMEOUT) as client:
        resp = await client.post(
            f"{FASTAPI_INTERNAL}/wait/{wait_type}/{wait_id}",
            json={},
        )
        resp.raise_for_status()
        return resp.json()


def _ok(data: dict) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps(data))]


def _err(msg: str) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps({"error": msg}))]


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------

TOOLS: list[Tool] = [
    Tool(
        name="generate_token",
        description="Authenticate with Pine Labs and obtain a Bearer token.",
        inputSchema={
            "type": "object",
            "properties": {},
            "required": [],
        },
    ),
    Tool(
        name="load_invoices",
        description=(
            "Parse a vendor invoice CSV for the given persona and upsert vendors into DB. "
            "Returns list of vendors, total invoice amount (INR), and count."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "csv_path": {"type": "string", "description": "Absolute or relative path to invoice CSV"},
                "persona": {"type": "string", "description": "Persona name (hospital|kirana)"},
            },
            "required": ["csv_path", "persona"],
        },
    ),
    Tool(
        name="get_account_balance",
        description="Pre-flight check: returns mock account balance and whether it is sufficient.",
        inputSchema={
            "type": "object",
            "properties": {},
            "required": [],
        },
    ),
    Tool(
        name="score_vendors",
        description=(
            "Score and rank vendors by priority for a given run and persona. "
            "Writes priority_score and action to the orders table. "
            "Returns ranked vendor list."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "persona": {"type": "string"},
            },
            "required": ["run_id", "persona"],
        },
    ),
    Tool(
        name="request_policy_approval",
        description=(
            "Gate: emits POLICY_GATE + CANVAS_STATE events and BLOCKS until a human approves "
            "via POST /approve/{run_id}. Returns {approved, approved_at}."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "vendors_summary": {
                    "type": "string",
                    "description": "Short textual summary of vendors to be paid",
                },
            },
            "required": ["run_id", "vendors_summary"],
        },
    ),
    Tool(
        name="request_escalation_decision",
        description=(
            "Emit ESCALATION event and BLOCK until a human decides capture or cancel for the vendor."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "vendor_id": {"type": "string"},
                "flag_type": {"type": "string", "description": "e.g. schedule_h, max_amount"},
                "details": {"type": "string"},
            },
            "required": ["vendor_id", "flag_type", "details"],
        },
    ),
    Tool(
        name="create_order",
        description=(
            "Create a Pine Labs order for a vendor, insert into DB, emit PIPELINE_STEP + VENDOR_STATE."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "vendor_id": {"type": "string"},
                "amount": {"type": "number", "description": "Amount in INR (rupees)"},
                "pre_auth": {"type": "boolean", "default": False},
            },
            "required": ["run_id", "vendor_id", "amount"],
        },
    ),
    Tool(
        name="create_payment",
        description=(
            "Create a payment for an existing order on a given rail. "
            "Rail options: neft, upi, upi_collect, upi_intent, netbanking, hosted_checkout, payment_link."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "order_id": {"type": "string", "description": "PRIYA internal order id (uuid)"},
                "rail": {"type": "string"},
                "amount": {"type": "number", "description": "Amount in INR"},
                "payer_vpa": {
                    "type": "string",
                    "description": "Required for upi_collect rail",
                },
            },
            "required": ["order_id", "rail", "amount"],
        },
    ),
    Tool(
        name="switch_rail_and_retry",
        description=(
            "Determine next rail from fallback chain, create a new payment, emit RAIL_SWITCH."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "order_id": {"type": "string"},
                "failed_rail": {"type": "string"},
                "failure_reason": {"type": "string"},
            },
            "required": ["order_id", "failed_rail", "failure_reason"],
        },
    ),
    Tool(
        name="capture_order",
        description="Capture a pre-authorised Pine Labs order.",
        inputSchema={
            "type": "object",
            "properties": {
                "order_id": {"type": "string"},
                "amount": {"type": "number", "description": "Capture amount in INR"},
            },
            "required": ["order_id", "amount"],
        },
    ),
    Tool(
        name="cancel_order",
        description="Cancel a Pine Labs order.",
        inputSchema={
            "type": "object",
            "properties": {
                "order_id": {"type": "string"},
            },
            "required": ["order_id"],
        },
    ),
    Tool(
        name="create_payment_link",
        description="Create a payment link as the last-resort fallback for an order.",
        inputSchema={
            "type": "object",
            "properties": {
                "order_id": {"type": "string"},
                "amount": {"type": "number"},
                "description": {"type": "string", "default": ""},
            },
            "required": ["order_id", "amount"],
        },
    ),
    Tool(
        name="run_settlements",
        description=(
            "Fetch settlements from Pine Labs for a date range, match to orders, "
            "write to settlements table."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "start_date": {"type": "string", "description": "YYYY-MM-DD"},
                "end_date": {"type": "string", "description": "YYYY-MM-DD"},
            },
            "required": ["run_id", "start_date", "end_date"],
        },
    ),
    Tool(
        name="run_reconciliation",
        description=(
            "Cross-check orders vs settlements, compute variance and MDR drift, "
            "write recon table, emit CANVAS_STATE:audit."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
            },
            "required": ["run_id"],
        },
    ),
    Tool(
        name="execute_sql_query",
        description="Run a read-only SELECT query on the PRIYA SQLite DB. Emits QUERY_RESULT event.",
        inputSchema={
            "type": "object",
            "properties": {
                "sql": {"type": "string"},
            },
            "required": ["sql"],
        },
    ),
    Tool(
        name="emit_event",
        description="Directly emit any WebSocket event (used for AGENT_NARRATION etc).",
        inputSchema={
            "type": "object",
            "properties": {
                "event_type": {"type": "string"},
                "payload": {"type": "object"},
            },
            "required": ["event_type", "payload"],
        },
    ),
    Tool(
        name="finalize_run",
        description=(
            "Compute final run metrics (paid/deferred/failed/float_saved/total), "
            "update runs table, emit RUN_SUMMARY."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
            },
            "required": ["run_id"],
        },
    ),
]


# ---------------------------------------------------------------------------
# MCP handlers
# ---------------------------------------------------------------------------

@app.list_tools()
async def list_tools() -> list[Tool]:
    return TOOLS


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        return await _dispatch(name, arguments)
    except Exception as exc:
        return _err(f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

async def _dispatch(name: str, args: dict) -> list[TextContent]:
    match name:
        case "generate_token":
            return await _generate_token(args)
        case "load_invoices":
            return await _load_invoices(args)
        case "get_account_balance":
            return await _get_account_balance(args)
        case "score_vendors":
            return await _score_vendors_tool(args)
        case "request_policy_approval":
            return await _request_policy_approval(args)
        case "request_escalation_decision":
            return await _request_escalation_decision(args)
        case "create_order":
            return await _create_order(args)
        case "create_payment":
            return await _create_payment(args)
        case "switch_rail_and_retry":
            return await _switch_rail_and_retry(args)
        case "capture_order":
            return await _capture_order(args)
        case "cancel_order":
            return await _cancel_order(args)
        case "create_payment_link":
            return await _create_payment_link_tool(args)
        case "run_settlements":
            return await _run_settlements(args)
        case "run_reconciliation":
            return await _run_reconciliation(args)
        case "execute_sql_query":
            return await _execute_sql_query(args)
        case "emit_event":
            return await _emit_event_tool(args)
        case "finalize_run":
            return await _finalize_run(args)
        case _:
            return _err(f"Unknown tool: {name}")


# --- AUTH & SETUP ---

async def _generate_token(_args: dict) -> list[TextContent]:
    result = await _pine.generate_token()
    token = result.get("access_token", "")
    # Store token in the run record so downstream tools can use it
    if RUN_ID:
        await _db.update_run_status(RUN_ID, "running", pine_token=token)
    return _ok({
        "access_token": token,
        "expires_at": result.get("expires_at"),
    })


async def _load_invoices(args: dict) -> list[TextContent]:
    csv_path: str = args["csv_path"]
    persona: str = args["persona"]

    vendors: list[dict] = []
    total_amount = 0.0

    with open(csv_path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            amount = float(row.get("amount", 0) or 0)
            vendor_id = row.get("vendor_id", str(uuid.uuid4()))

            db_vendor = {
                "id": vendor_id,
                "name": row.get("vendor_name", ""),
                "persona": persona,
                "category": row.get("category", ""),
                "upi_id": row.get("upi_id", None),
                "bank_account": row.get("bank_account", None),
                "ifsc": row.get("ifsc", None),
                "preferred_rail": row.get("preferred_rail", "neft"),
                "credit_days": int(row.get("credit_days", 0) or 0),
                "vendor_type": row.get("vendor_type", ""),
                "drug_schedule": row.get("drug_schedule", None) or None,
                "is_compliant": row.get("is_compliant", "true").lower() == "true",
                "created_at": _now_iso(),
            }
            await _db.upsert_vendor(db_vendor)

            # Keep extra fields in the returned vendor dict for scoring
            vendor = dict(db_vendor)
            vendor["amount"] = amount
            vendor["invoice_date"] = row.get("invoice_date", None)
            vendor["due_date"] = row.get("due_date", None)
            total_amount += amount
            vendors.append(vendor)

    return _ok({
        "vendors": vendors,
        "total_amount": total_amount,
        "count": len(vendors),
    })


async def _get_account_balance(_args: dict) -> list[TextContent]:
    return _ok({
        "balance": 10_00_000.0,  # Rs 10 lakh mock balance
        "currency": "INR",
        "sufficient": True,
    })


# --- SCORING & GATES ---

async def _score_vendors_tool(args: dict) -> list[TextContent]:
    run_id: str = args["run_id"]
    persona: str = args["persona"]

    persona_config = _load_persona(persona)
    vendors = await _db.get_vendors_by_persona(persona)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    ranked = _score_vendors(vendors, persona_config, today)

    # Persist priority to orders table (update existing orders for this run)
    for v in ranked:
        orders = await _db.get_orders_by_run(run_id)
        for order in orders:
            if order["vendor_id"] == v["id"]:
                await _db.update_order_status(
                    order["id"],
                    order["pine_status"],
                    priority_score=v["priority_score"],
                    priority_reason=v.get("priority_reason", ""),
                    action=v.get("action", "pay"),
                )

    return _ok({"ranked_vendors": ranked})


async def _request_policy_approval(args: dict) -> list[TextContent]:
    run_id: str = args["run_id"]
    vendors_summary: str = args["vendors_summary"]

    await _emit_event("POLICY_GATE", {
        "run_id": run_id,
        "vendors_summary": vendors_summary,
        "requested_at": _now_iso(),
    })
    await _emit_event("CANVAS_STATE", {
        "state": "policy_gate",
        "run_id": run_id,
    })

    result = await _wait_for_approval("approve", run_id)
    approved_at = result.get("approved_at", _now_iso())

    await _db.update_run_status(run_id, "approved", approved_at=approved_at)

    return _ok({"approved": True, "approved_at": approved_at})


async def _request_escalation_decision(args: dict) -> list[TextContent]:
    vendor_id: str = args["vendor_id"]
    flag_type: str = args["flag_type"]
    details: str = args["details"]
    esc_id = str(uuid.uuid4())

    await _emit_event("ESCALATION", {
        "escalation_id": esc_id,
        "vendor_id": vendor_id,
        "flag_type": flag_type,
        "details": details,
        "requested_at": _now_iso(),
    })

    result = await _wait_for_approval("escalation", esc_id)
    decision = result.get("decision", "cancel")

    return _ok({"decision": decision, "escalation_id": esc_id})


# --- PINE LABS OPS ---

async def _create_order(args: dict) -> list[TextContent]:
    run_id: str = args["run_id"]
    vendor_id: str = args["vendor_id"]
    amount: float = args["amount"]
    pre_auth: bool = args.get("pre_auth", False)

    run = await _db.get_run(run_id)
    if not run:
        return _err(f"Run not found: {run_id}")
    token: str = run["pine_token"]

    vendor = await _db.get_vendor(vendor_id)
    if not vendor:
        return _err(f"Vendor not found: {vendor_id}")

    mor = f"PRIYA-{run_id[:8]}-{vendor_id[:8]}-{uuid.uuid4().hex[:6]}".upper()

    pine_resp = await _pine.create_order(
        token=token,
        merchant_order_reference=mor,
        amount_paisa=_rupees_to_paisa(amount),
        pre_auth=pre_auth,
    )
    pine_order = pine_resp.get("data", pine_resp)
    pine_order_id = pine_order.get("order_id", "")

    order_id = str(uuid.uuid4())
    order = {
        "id": order_id,
        "run_id": run_id,
        "vendor_id": vendor_id,
        "pine_order_id": pine_order_id,
        "merchant_order_reference": mor,
        "amount": amount,
        "priority_score": 0,
        "priority_reason": "",
        "action": "pay",
        "pre_auth": pre_auth,
        "pine_status": pine_order.get("status", "CREATED"),
        "escalation_flag": None,
        "defer_reason": None,
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
    }
    await _db.insert_order(order)

    await _emit_event("PIPELINE_STEP", {
        "step": "create_order",
        "run_id": run_id,
        "vendor_id": vendor_id,
        "pine_order_id": pine_order_id,
        "mor": mor,
        "amount": amount,
        "pre_auth": pre_auth,
    })
    await _emit_event("VENDOR_STATE", {
        "vendor_id": vendor_id,
        "state": "order_created",
        "pine_order_id": pine_order_id,
    })

    return _ok({
        "pine_order_id": pine_order_id,
        "order_id": order_id,
        "status": pine_order.get("status", "CREATED"),
        "mor": mor,
    })


async def _build_payment_via_pine(
    token: str,
    pine_order_id: str,
    rail: str,
    amount_paisa: int,
    merchant_payment_reference: str,
    payer_vpa: str | None = None,
) -> dict:
    """Route to the correct pine_client method based on rail."""
    match rail:
        case "upi_intent":
            return await _pine.create_payment_upi_intent(
                token, pine_order_id, merchant_payment_reference, amount_paisa
            )
        case "upi_collect":
            return await _pine.create_payment_upi_collect(
                token, pine_order_id, merchant_payment_reference, amount_paisa,
                payer_vpa=payer_vpa or "",
            )
        case "upi":
            # Default UPI = intent
            return await _pine.create_payment_upi_intent(
                token, pine_order_id, merchant_payment_reference, amount_paisa
            )
        case "netbanking":
            return await _pine.create_payment_netbanking(
                token, pine_order_id, merchant_payment_reference, amount_paisa
            )
        case "neft":
            # NEFT modelled as netbanking in Pine Labs UAT
            return await _pine.create_payment_netbanking(
                token, pine_order_id, merchant_payment_reference, amount_paisa
            )
        case "hosted_checkout":
            run_resp = await _pine.get_order(token, pine_order_id)
            order_data = run_resp.get("data", run_resp)
            mor = order_data.get("merchant_order_reference", merchant_payment_reference)
            return await _pine.hosted_checkout(
                token, mor, amount_paisa, callback_url=f"http://localhost:8000/webhook/checkout"
            )
        case "payment_link":
            return await _pine.create_payment_link(
                token, amount_paisa, merchant_payment_reference
            )
        case _:
            return await _pine.create_payment_upi_intent(
                token, pine_order_id, merchant_payment_reference, amount_paisa
            )


async def _create_payment(args: dict) -> list[TextContent]:
    order_id: str = args["order_id"]
    rail: str = args["rail"]
    amount: float = args["amount"]
    payer_vpa: str | None = args.get("payer_vpa")

    order = await _db._fetchone("SELECT * FROM orders WHERE id = ?", (order_id,))
    if not order:
        return _err(f"Order not found: {order_id}")

    run = await _db.get_run(order["run_id"])
    if not run:
        return _err(f"Run not found: {order['run_id']}")
    token: str = run["pine_token"]

    existing_payments = await _db.get_payments_by_order(order_id)
    attempt_number = len(existing_payments) + 1

    mpr = f"PAY-{order_id[:8]}-{attempt_number}-{uuid.uuid4().hex[:4]}".upper()

    pine_resp = await _build_payment_via_pine(
        token=token,
        pine_order_id=order["pine_order_id"],
        rail=rail,
        amount_paisa=_rupees_to_paisa(amount),
        merchant_payment_reference=mpr,
        payer_vpa=payer_vpa,
    )
    pine_payment = pine_resp.get("data", pine_resp)
    pine_payment_id = pine_payment.get("payment_id", pine_payment.get("order_id", ""))
    status = pine_payment.get("status", "PROCESSED")

    payment_id = str(uuid.uuid4())
    payment = {
        "id": payment_id,
        "run_id": order["run_id"],
        "order_id": order_id,
        "vendor_id": order["vendor_id"],
        "pine_order_id": order["pine_order_id"],
        "pine_payment_id": pine_payment_id,
        "merchant_payment_reference": mpr,
        "amount": amount,
        "rail": rail,
        "attempt_number": attempt_number,
        "pine_status": status,
        "failure_reason": pine_payment.get("failure_reason"),
        "recovery_action": None,
        "webhook_event": None,
        "request_id": pine_payment.get("payment_id", ""),
        "initiated_at": _now_iso(),
        "confirmed_at": _now_iso() if status in ("PROCESSED", "SUCCESS") else None,
    }
    await _db.insert_payment(payment)

    if status in ("PROCESSED", "SUCCESS"):
        await _db.update_order_status(order_id, "PROCESSED")

    await _emit_event("VENDOR_STATE", {
        "vendor_id": order["vendor_id"],
        "state": "payment_" + status.lower(),
        "pine_payment_id": pine_payment_id,
        "rail": rail,
        "attempt_number": attempt_number,
    })

    return _ok({
        "pine_payment_id": pine_payment_id,
        "payment_id": payment_id,
        "status": status,
        "attempt_number": attempt_number,
    })


async def _switch_rail_and_retry(args: dict) -> list[TextContent]:
    order_id: str = args["order_id"]
    failed_rail: str = args["failed_rail"]
    failure_reason: str = args["failure_reason"]

    fallback_chain = RAIL_FALLBACKS.get(failed_rail, [])
    if not fallback_chain:
        return _err(f"No more fallback rails after {failed_rail}")

    new_rail = fallback_chain[0]

    order = await _db._fetchone("SELECT * FROM orders WHERE id = ?", (order_id,))
    if not order:
        return _err(f"Order not found: {order_id}")

    await _emit_event("RAIL_SWITCH", {
        "order_id": order_id,
        "vendor_id": order["vendor_id"],
        "failed_rail": failed_rail,
        "new_rail": new_rail,
        "failure_reason": failure_reason,
    })

    pay_result = await _create_payment({
        "order_id": order_id,
        "rail": new_rail,
        "amount": order["amount"],
    })

    pay_data = json.loads(pay_result[0].text)
    return _ok({
        "new_rail": new_rail,
        "pine_payment_id": pay_data.get("pine_payment_id"),
        "status": pay_data.get("status"),
    })


async def _capture_order(args: dict) -> list[TextContent]:
    order_id: str = args["order_id"]
    amount: float = args["amount"]

    order = await _db._fetchone("SELECT * FROM orders WHERE id = ?", (order_id,))
    if not order:
        return _err(f"Order not found: {order_id}")

    run = await _db.get_run(order["run_id"])
    token: str = run["pine_token"]

    cap_ref = f"CAP-{order_id[:8]}-{uuid.uuid4().hex[:6]}".upper()
    await _pine.capture_order(
        token=token,
        order_id=order["pine_order_id"],
        merchant_capture_reference=cap_ref,
        capture_amount_paisa=_rupees_to_paisa(amount),
    )
    await _db.update_order_status(order_id, "CAPTURED")

    return _ok({"status": "CAPTURED", "order_id": order_id})


async def _cancel_order(args: dict) -> list[TextContent]:
    order_id: str = args["order_id"]

    order = await _db._fetchone("SELECT * FROM orders WHERE id = ?", (order_id,))
    if not order:
        return _err(f"Order not found: {order_id}")

    run = await _db.get_run(order["run_id"])
    token: str = run["pine_token"]

    await _pine.cancel_order(token=token, order_id=order["pine_order_id"])
    await _db.update_order_status(order_id, "CANCELLED")

    return _ok({"status": "CANCELLED", "order_id": order_id})


async def _create_payment_link_tool(args: dict) -> list[TextContent]:
    order_id: str = args["order_id"]
    amount: float = args["amount"]
    description: str = args.get("description", "")

    order = await _db._fetchone("SELECT * FROM orders WHERE id = ?", (order_id,))
    if not order:
        return _err(f"Order not found: {order_id}")

    run = await _db.get_run(order["run_id"])
    token: str = run["pine_token"]

    ref = f"LINK-{order_id[:8]}-{uuid.uuid4().hex[:6]}".upper()
    pine_resp = await _pine.create_payment_link(
        token=token,
        amount_paisa=_rupees_to_paisa(amount),
        merchant_payment_link_reference=ref,
        description=description,
    )
    link_data = pine_resp.get("data", pine_resp)

    return _ok({
        "payment_link": link_data.get("payment_link_url", ""),
        "payment_link_id": link_data.get("payment_link_id", ""),
    })


# --- SETTLEMENT & RECON ---

async def _run_settlements(args: dict) -> list[TextContent]:
    run_id: str = args["run_id"]
    start_date: str = args["start_date"]
    end_date: str = args["end_date"]

    run = await _db.get_run(run_id)
    if not run:
        return _err(f"Run not found: {run_id}")
    token: str = run["pine_token"]

    orders = await _db.get_orders_by_run(run_id)
    pine_order_ids = {o["pine_order_id"] for o in orders}

    result = await _pine.get_all_settlements(token, start_date, end_date)
    raw_settlements = result.get("data", [])

    matched_count = 0
    written: list[dict] = []

    for s in raw_settlements:
        utr = s.get("utr", "")
        net_amount_paisa = s.get("net_amount", 0)
        gross_amount_paisa = s.get("gross_amount", 0)
        mdr_paisa = s.get("mdr_amount", 0)

        # Match to a specific order if possible
        matched_pine_order_id = None
        for oid in s.get("orders", []):
            if oid in pine_order_ids:
                matched_pine_order_id = oid
                break

        settlement = {
            "id": str(uuid.uuid4()),
            "run_id": run_id,
            "pine_order_id": matched_pine_order_id or "",
            "pine_settlement_id": utr,
            "utr_number": utr,
            "bank_account": s.get("bank_account", ""),
            "last_processed_date": s.get("settlement_date", ""),
            "expected_amount": gross_amount_paisa / 100.0,
            "settled_amount": net_amount_paisa / 100.0,
            "platform_fee": mdr_paisa / 100.0,
            "total_deduction_amount": mdr_paisa / 100.0,
            "refund_debit": 0.0,
            "fee_flagged": False,
            "status": "SETTLED",
            "settled_at": s.get("settlement_date", _now_iso()),
            "created_at": _now_iso(),
        }
        await _db.insert_settlement(settlement)
        written.append(settlement)

        if matched_pine_order_id:
            matched_count += 1

    return _ok({
        "settlements": written,
        "matched_count": matched_count,
    })


async def _run_reconciliation(args: dict) -> list[TextContent]:
    run_id: str = args["run_id"]

    run = await _db.get_run(run_id)
    if not run:
        return _err(f"Run not found: {run_id}")

    persona = run.get("persona", "")
    persona_config = _load_persona(persona) if persona else {}
    mdr_contracted = persona_config.get("mdr_rate_contracted", 0.018)

    orders = await _db.get_orders_by_run(run_id)
    settlements = await _db.get_settlements_by_run(run_id)

    # Index settlements by pine_order_id for O(1) lookup
    sett_by_order: dict[str, dict] = {}
    for s in settlements:
        if s["pine_order_id"]:
            sett_by_order[s["pine_order_id"]] = s

    recons: list[dict] = []
    total_variance = 0.0
    mdr_drift_count = 0

    for order in orders:
        payments = await _db.get_payments_by_order(order["id"])
        successful_payment = next(
            (p for p in payments if p["pine_status"] in ("PROCESSED", "SUCCESS")), None
        )
        sett = sett_by_order.get(order["pine_order_id"])

        invoice_amount = order["amount"]
        paid_amount = successful_payment["amount"] if successful_payment else 0.0
        settled_amount = sett["settled_amount"] if sett else 0.0
        variance = round(paid_amount - settled_amount, 2)
        total_variance += variance

        mdr_actual = 0.0
        mdr_drift_flagged = False
        if paid_amount > 0 and sett:
            mdr_actual = round(sett["platform_fee"] / paid_amount, 6) if paid_amount else 0.0
            if abs(mdr_actual - mdr_contracted) > 0.002:
                mdr_drift_flagged = True
                mdr_drift_count += 1

        outcome = "matched"
        if not successful_payment:
            outcome = "failed"
        elif not sett:
            outcome = "pending_settlement"
        elif variance != 0:
            outcome = "variance"

        recon = {
            "id": str(uuid.uuid4()),
            "run_id": run_id,
            "order_id": order["id"],
            "payment_id": successful_payment["id"] if successful_payment else None,
            "settlement_id": sett["id"] if sett else None,
            "vendor_id": order["vendor_id"],
            "pine_order_id": order["pine_order_id"],
            "merchant_order_reference": order["merchant_order_reference"],
            "utr_number": sett["utr_number"] if sett else None,
            "persona": persona,
            "invoice_amount": invoice_amount,
            "paid_amount": paid_amount,
            "settled_amount": settled_amount,
            "variance": variance,
            "mdr_rate_actual": mdr_actual,
            "mdr_rate_contracted": mdr_contracted,
            "mdr_drift_flagged": mdr_drift_flagged,
            "rail_used": successful_payment["rail"] if successful_payment else None,
            "retries": len(payments),
            "outcome": outcome,
            "pre_auth_used": order["pre_auth"],
            "agent_reasoning": order.get("priority_reason", ""),
            "ca_notes": None,
            "created_at": _now_iso(),
        }
        await _db.insert_reconciliation(recon)
        recons.append(recon)

    summary = {
        "total_orders": len(orders),
        "matched": sum(1 for r in recons if r["outcome"] == "matched"),
        "variance_count": sum(1 for r in recons if r["outcome"] == "variance"),
        "failed_count": sum(1 for r in recons if r["outcome"] == "failed"),
        "pending_settlement_count": sum(1 for r in recons if r["outcome"] == "pending_settlement"),
        "total_variance_inr": round(total_variance, 2),
        "mdr_drift_count": mdr_drift_count,
    }

    await _emit_event("CANVAS_STATE", {
        "state": "audit",
        "run_id": run_id,
        "summary": summary,
    })

    return _ok({"reconciliations": recons, "summary": summary})


# --- QUERIES & EVENTS ---

async def _execute_sql_query(args: dict) -> list[TextContent]:
    sql: str = args["sql"]
    rows = await _db.execute_query(sql)

    columns = list(rows[0].keys()) if rows else []

    await _emit_event("QUERY_RESULT", {
        "sql": sql,
        "columns": columns,
        "row_count": len(rows),
    })

    return _ok({
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
    })


async def _emit_event_tool(args: dict) -> list[TextContent]:
    event_type: str = args["event_type"]
    payload: dict = args["payload"]
    await _emit_event(event_type, payload)
    return _ok({"sent": True})


async def _finalize_run(args: dict) -> list[TextContent]:
    run_id: str = args["run_id"]

    orders = await _db.get_orders_by_run(run_id)

    paid = 0
    deferred = 0
    failed = 0
    float_saved = 0.0
    total = 0.0

    for order in orders:
        total += order["amount"]
        action = order.get("action", "pay")
        status = order.get("pine_status", "")

        if action == "defer":
            deferred += order["amount"]
        elif status in ("PROCESSED", "CAPTURED", "SUCCESS"):
            paid += order["amount"]
        else:
            failed += order["amount"]

    # Compute float_saved from credit float analysis for kirana persona
    run = await _db.get_run(run_id)
    if run and run.get("persona") == "kirana":
        vendors = await _db.get_vendors_by_persona("kirana")
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        for v in vendors:
            credit_days = int(v.get("credit_days", 0) or 0)
            if credit_days > 0:
                # Find deferred orders for this vendor
                deferred_orders = [
                    o for o in orders
                    if o["vendor_id"] == v["id"] and o.get("action") == "defer"
                ]
                for o in deferred_orders:
                    finfo = compute_credit_float(
                        vendor={**v, "amount": o["amount"]},
                        invoice_date=today_str,
                        today=today_str,
                        cost_of_capital=0.12,
                    )
                    float_saved += finfo["float_saved"]

    metrics = {
        "paid": round(paid, 2),
        "deferred": round(deferred, 2),
        "failed": round(failed, 2),
        "float_saved": round(float_saved, 2),
        "total": round(total, 2),
    }

    await _db.update_run_status(
        run_id,
        "completed",
        paid_amount=metrics["paid"],
        deferred_amount=metrics["deferred"],
        float_saved=metrics["float_saved"],
    )

    await _emit_event("RUN_SUMMARY", {
        "run_id": run_id,
        **metrics,
    })

    return _ok(metrics)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main() -> None:
    await _db.init()
    try:
        async with stdio_server() as (read_stream, write_stream):
            await app.run(read_stream, write_stream, app.create_initialization_options())
    finally:
        await _db.close()


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
