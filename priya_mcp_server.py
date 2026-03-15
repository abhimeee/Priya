"""
PRIYA custom MCP server — 20 domain tools exposed over stdio JSON-RPC.

All monetary values flowing through MCP tools are in RUPEES (float).
Conversion to PAISA (int) happens at the pine_client boundary.

WS events are emitted via HTTP POST to http://localhost:8000/internal/event.
Blocking tools POST to http://localhost:8000/internal/wait/{type}/{id}
and wait until approval arrives.

Run as subprocess via the agent SDK (stdio transport).
"""
from __future__ import annotations

import asyncio
import csv
import json
import os
import uuid
from datetime import date, datetime, timezone, timedelta
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

# Payout simulation mode — when True, payout disbursement calls are simulated
# locally instead of hitting Pine Labs Payouts API (which requires merchant
# enablement that MID 121495 doesn't have on UAT).  Auth, orders, and
# settlements still use the real Pine Labs API.
PINE_PAYOUT_SIM = os.getenv("PINE_PAYOUT_SIM", "false").lower() == "true"

# Rail fallback chains — payment_link is most reliable on Pine Labs UAT
RAIL_FALLBACKS: dict[str, list[str]] = {
    "neft": ["payment_link", "hosted_checkout"],
    "upi": ["payment_link", "hosted_checkout"],
    "upi_collect": ["upi_intent", "hosted_checkout", "payment_link"],
    "upi_intent": ["hosted_checkout", "payment_link"],
    "netbanking": ["upi_intent", "hosted_checkout", "payment_link"],
    "hosted_checkout": ["payment_link"],
    "payment_link": [],
}

# Payout rail fallback chain: IMPS → NEFT → UPI
PAYOUT_RAIL_FALLBACKS: dict[str, list[str]] = {
    "IMPS": ["NEFT", "UPI"],
    "NEFT": ["UPI"],
    "UPI": [],
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


def _business_days_between(start_str: str, end_str: str) -> int:
    """Return number of business days (Mon-Fri) between two ISO date strings."""
    try:
        def _parse(s: str) -> date:
            return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
        start = _parse(start_str)
        end = _parse(end_str)
        if end < start:
            start, end = end, start
        days = 0
        current = start
        while current <= end:
            if current.weekday() < 5:  # Mon-Fri
                days += 1
            current += timedelta(days=1)
        # Subtract 1 so "same day" = 0 delay
        return max(0, days - 1)
    except Exception:
        return 0


def _run_recon_checks(
    recon: dict,
    sett: dict | None,
    all_settlements_for_utr: list[dict],
    orders_in_run: list[dict],
    sett_missing: bool = False,
) -> tuple[list[dict], str, int]:
    """
    Run all 10 reconciliation checks covering both legs of the payment flow:
      Collection (owner→PRIYA): Checks 1-8 verify Pine Labs settlement
      Disbursement (PRIYA→vendor): Check 10 verifies payout to vendor
      Cross-check: Check 9 verifies invoice vs collected amount
    Returns (checks_list, recon_status, settlement_delay_days).
    recon_status: 'MATCHED' | 'MISMATCH' | 'WARNING' | 'PENDING'
    """
    checks: list[dict] = []

    paid_amount = recon.get("paid_amount", 0.0) or 0.0
    settled_amount = recon.get("settled_amount", 0.0) or 0.0
    # Use vendor's proportional fee (passed from caller), not batch-level sett.platform_fee
    platform_fee = recon.get("vendor_fee", 0.0) or ((sett.get("platform_fee", 0.0) or 0.0) if sett else 0.0)
    mdr_actual = recon.get("mdr_rate_actual") or 0.0
    mdr_contracted = recon.get("mdr_rate_contracted") or 0.0
    rail_used = recon.get("rail_used") or ""
    bank_credit_amount = recon.get("bank_credit_amount")

    # Check 1 (BLOCKING): Settlement Amount Match
    if sett_missing and paid_amount > 0:
        # Settlement hasn't arrived yet (T+1 delay) — defer, don't fail
        c1_passed = None
        c1_detail = "Settlement pending (T+1 delay)"
    else:
        expected_net = round(paid_amount - platform_fee, 2)
        c1_passed = abs(settled_amount - expected_net) < 0.01
        c1_detail = (
            f"settled={settled_amount}, paid={paid_amount}, fee={platform_fee}, "
            f"expected_net={expected_net}"
        ) if not c1_passed else f"settled_amount={settled_amount} matches paid-fee={expected_net}"
    checks.append({
        "check_id": 1,
        "name": "settlement_amount_match",
        "severity": "blocking",
        "passed": c1_passed,
        "detail": c1_detail,
    })

    # Check 2 (BLOCKING): Bank Credit Match
    # Verifies: did PRIYA's bank account actually receive what Pine Labs claims
    # they settled?  Requires bank statement data (API feed, CSV upload, or webhook).
    # passed=None until bank_credit_amount is populated from an external source.
    if bank_credit_amount is None:
        c2_passed = None
        c2_detail = "Awaiting bank statement data (AA feed / CSV upload / bank webhook)"
    else:
        bank_delta = round(bank_credit_amount - settled_amount, 2)
        c2_passed = abs(bank_delta) < 0.01
        c2_detail = (
            f"bank_credit={bank_credit_amount}, settled={settled_amount}, delta={bank_delta}"
        )
    checks.append({
        "check_id": 2,
        "name": "bank_credit_match",
        "severity": "blocking",
        "passed": c2_passed,
        "detail": c2_detail,
    })

    # Check 3 (WARNING): MDR Rate Drift
    # Look up the contracted rate for this specific rail if available
    mdr_rates_by_rail = recon.get("mdr_rates_by_rail") or {}
    mdr_expected = mdr_rates_by_rail.get(rail_used, mdr_contracted)

    upi_zero_mdr_rails = ("upi", "upi_intent", "upi_collect", "payment_link")
    if rail_used in upi_zero_mdr_rails and mdr_actual == 0.0:
        # UPI zero-MDR is correct per NPCI mandate — not a drift
        c3_passed = True
        c3_detail = f"Zero MDR expected for {rail_used}"
    else:
        mdr_drift = abs(mdr_actual - mdr_expected) > 0.001
        c3_passed = not mdr_drift
        c3_detail = (
            f"actual={mdr_actual:.6f}, contracted={mdr_expected:.6f}, "
            f"drift={abs(mdr_actual - mdr_expected):.6f}"
        )
    checks.append({
        "check_id": 3,
        "name": "mdr_rate_drift",
        "severity": "warning",
        "passed": c3_passed,
        "detail": c3_detail,
    })

    # Check 4 (WARNING): Settlement Delay
    delay_days = 0
    if sett and sett.get("last_processed_date") and sett.get("settled_at"):
        delay_days = _business_days_between(
            sett["last_processed_date"], sett["settled_at"]
        )
    c4_passed = delay_days <= 2
    checks.append({
        "check_id": 4,
        "name": "settlement_delay",
        "severity": "warning",
        "passed": c4_passed,
        "detail": f"settlement_delay_days={delay_days} (threshold=2 business days)",
    })

    # Check 5 (INFO): Refund Impact
    refund_debit = (sett.get("refund_debit", 0.0) or 0.0) if sett else 0.0
    c5_passed = refund_debit == 0.0
    checks.append({
        "check_id": 5,
        "name": "refund_impact",
        "severity": "info",
        "passed": c5_passed,
        "detail": f"refund_debit={refund_debit}",
    })

    # Check 6 (INFO): Order Sum Check — sum of all order amounts in batch == settlement expected_amount (gross)
    if sett and orders_in_run:
        # expected_amount is now the batch total (sum of all vendor amounts)
        settlement_gross = sett.get("expected_amount", 0.0) or 0.0
        # All orders sharing this pine_order_id
        orders_in_batch = [
            o for o in orders_in_run
            if o.get("pine_order_id") == sett.get("pine_order_id")
        ]
        order_sum = round(sum(o.get("amount", 0.0) for o in orders_in_batch), 2)
        c6_passed = abs(order_sum - settlement_gross) < 0.01 if orders_in_batch else True
        checks.append({
            "check_id": 6,
            "name": "order_sum_check",
            "severity": "info",
            "passed": c6_passed,
            "detail": f"batch_order_sum={order_sum}, settlement_gross={settlement_gross}, vendors_in_batch={len(orders_in_batch)}",
        })
    else:
        checks.append({
            "check_id": 6,
            "name": "order_sum_check",
            "severity": "info",
            "passed": True,
            "detail": "no settlement to compare",
        })

    # Check 7 (INFO): UPI Zero MDR
    c7_passed = not (rail_used.upper().startswith("UPI") and platform_fee > 0)
    checks.append({
        "check_id": 7,
        "name": "upi_zero_mdr",
        "severity": "info",
        "passed": c7_passed,
        "detail": f"rail={rail_used}, platform_fee={platform_fee}",
    })

    # Check 8 (INFO): Duplicate UTR
    utr_count = len(all_settlements_for_utr)
    distinct_order_ids = set(
        s.get("pine_order_id") for s in all_settlements_for_utr if s.get("pine_order_id")
    )
    # If all settlements share the same pine_order_id, it's a batch — not a duplicate
    is_batch = len(distinct_order_ids) <= 1
    c8_passed = utr_count <= 1 or is_batch
    checks.append({
        "check_id": 8,
        "name": "duplicate_utr",
        "severity": "info",
        "passed": c8_passed,
        "detail": (
            f"settlements_with_same_utr={utr_count}, distinct_order_ids={len(distinct_order_ids)}"
            + (", batch_order=true" if is_batch and utr_count > 1 else "")
        ),
    })

    # Check 9 (BLOCKING): Invoice vs Paid Amount
    invoice_amount = recon.get("invoice_amount", 0.0) or 0.0
    if paid_amount == 0:
        c9_passed = None  # No payment made yet — defer
        c9_detail = "No payment made yet, deferring invoice check"
    else:
        c9_passed = abs(invoice_amount - paid_amount) < 0.01
        c9_detail = (
            f"invoice_amount={invoice_amount}, paid_amount={paid_amount}"
            if not c9_passed
            else f"invoice_amount={invoice_amount} matches paid_amount={paid_amount}"
        )
    checks.append({
        "check_id": 9,
        "name": "invoice_vs_paid",
        "severity": "blocking",
        "passed": c9_passed,
        "detail": c9_detail,
    })

    # Check 10 (BLOCKING): Payout Verification — did the vendor receive the disbursement?
    # In PRIYA's model: owner pays PRIYA (collection), PRIYA pays vendors (payout).
    # This check verifies the payout leg completed successfully.
    payout_info = recon.get("payout")
    if payout_info is None:
        # No payout record yet — might be pre-payout stage or payout not initiated
        c10_passed = None
        c10_detail = "Payout not yet initiated to vendor"
    elif payout_info.get("status") == "SUCCESS":
        payout_amount = float(payout_info.get("amount", 0))
        c10_passed = abs(payout_amount - invoice_amount) < 0.01
        c10_detail = (
            f"payout={payout_amount}, invoice={invoice_amount}, "
            f"mode={payout_info.get('mode', '?')}, "
            f"utr={payout_info.get('bank_txn_reference_id', 'N/A')}"
        )
    elif payout_info.get("status") == "FAILED":
        c10_passed = False
        c10_detail = f"Payout FAILED: {payout_info.get('message', 'unknown error')}"
    else:
        c10_passed = None
        c10_detail = f"Payout in progress: status={payout_info.get('status', '?')}"
    checks.append({
        "check_id": 10,
        "name": "payout_to_vendor",
        "severity": "blocking",
        "passed": c10_passed,
        "detail": c10_detail,
    })

    # Determine recon_status
    # passed=None means "not yet determined" (deferred) — neither blocking fail nor pass
    blocking_checks = [c for c in checks if c["severity"] == "blocking"]
    blocking_failed = any(c["passed"] is False for c in blocking_checks)
    blocking_deferred = any(c["passed"] is None for c in blocking_checks)
    warning_failed = any(
        c for c in checks
        if c["severity"] == "warning" and c["passed"] is False
    )

    if blocking_failed:
        recon_status = "MISMATCH"
    elif blocking_deferred:
        recon_status = "PENDING"
    elif warning_failed:
        recon_status = "WARNING"
    else:
        recon_status = "MATCHED"

    return checks, recon_status, delay_days


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
        name="create_batch_order",
        description=(
            "Create a SINGLE Pine Labs order for ALL vendors in a run. "
            "Groups all vendor invoices into one order with the total amount. "
            "Creates individual DB order records per vendor linked to the same pine_order_id. "
            "Emits PIPELINE_STEP + VENDOR_STATE for all vendors. "
            "Use this instead of creating separate orders per vendor."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "pre_auth": {"type": "boolean", "default": False, "description": "Set true for schedule_h vendors"},
            },
            "required": ["run_id"],
        },
    ),
    Tool(
        name="create_order",
        description=(
            "Create a Pine Labs order for a SINGLE vendor. "
            "Prefer create_batch_order for bulk runs. Use this only for individual re-orders."
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
        name="create_batch_payment",
        description=(
            "Execute payment for a batch order on the preferred rail. "
            "Pays the full order amount in one Pine Labs API call. "
            "Updates all vendor statuses. On failure, use switch_rail_and_retry."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "rail": {"type": "string", "description": "Payment rail: neft, upi, upi_intent, netbanking, hosted_checkout, payment_link"},
            },
            "required": ["run_id", "rail"],
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
        name="await_payment_confirmation",
        description=(
            "Wait for Pine Labs webhook to confirm payment status after payment links are generated. "
            "Polls the DB for payment status changes. Returns when all payments are confirmed "
            "(PROCESSED/SUCCESS) or failed, or after timeout. MUST be called after create_batch_payment "
            "when using payment_link rail, BEFORE proceeding to settlements."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "timeout_seconds": {"type": "integer", "default": 600, "description": "Max wait time in seconds (default 600 = 10 min)"},
                "poll_interval_seconds": {"type": "integer", "default": 10, "description": "How often to check DB (default 10s)"},
            },
            "required": ["run_id"],
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
    # --- PAYOUT TOOLS (Pine Labs Payouts API v3) ---
    Tool(
        name="execute_payouts",
        description=(
            "Execute payouts for all approved vendors in a run. Uses IMPS as primary rail, "
            "falls back to NEFT, then UPI. Creates individual payout per vendor via "
            "Pine Labs Payouts API."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "mode": {
                    "type": "string",
                    "default": "IMPS",
                    "description": "Payout mode: IMPS, NEFT, RTGS, or UPI",
                },
            },
            "required": ["run_id"],
        },
    ),
    Tool(
        name="poll_payout_status",
        description=(
            "Poll Pine Labs for payout status updates. Checks all pending payouts in a run "
            "and updates DB. Call this after execute_payouts to track completion."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "timeout_seconds": {
                    "type": "integer",
                    "default": 120,
                    "description": "Max wait time in seconds (default 120)",
                },
                "poll_interval_seconds": {
                    "type": "integer",
                    "default": 5,
                    "description": "How often to poll Pine Labs (default 5s)",
                },
            },
            "required": ["run_id"],
        },
    ),
    Tool(
        name="retry_failed_payout",
        description=(
            "Retry a failed payout with a different rail. Tries NEFT if IMPS failed, "
            "then UPI as last resort."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "vendor_id": {"type": "string"},
                "new_mode": {
                    "type": "string",
                    "description": "New payout mode: IMPS, NEFT, or UPI",
                },
            },
            "required": ["run_id", "vendor_id", "new_mode"],
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
        case "create_batch_order":
            return await _create_batch_order(args)
        case "create_order":
            return await _create_order(args)
        case "create_batch_payment":
            return await _create_batch_payment(args)
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
        case "await_payment_confirmation":
            return await _await_payment_confirmation(args)
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
        case "execute_payouts":
            return await _execute_payouts(args)
        case "poll_payout_status":
            return await _poll_payout_status(args)
        case "retry_failed_payout":
            return await _retry_failed_payout(args)
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
                "amount": amount,
                "invoice_number": row.get("invoice_number", None),
                "invoice_date": row.get("invoice_date", None),
                "due_date": row.get("due_date", None),
                "items": int(row.get("items", 0) or 0),
                "created_at": _now_iso(),
            }
            await _db.upsert_vendor(db_vendor)

            vendor = dict(db_vendor)
            total_amount += amount
            vendors.append(vendor)

    # Emit VENDOR_STATE so frontend populates the vendor table
    await _emit_event("VENDOR_STATE", {
        "run_id": RUN_ID,
        "vendors": [
            {
                "vendor_id": v["id"],
                "name": v["name"],
                "amount": v["amount"],
                "rail": v.get("preferred_rail", "neft"),
                "state": "CREATED",
            }
            for v in vendors
        ],
    })

    await _emit_event("PIPELINE_STEP", {
        "run_id": RUN_ID,
        "step": "LOAD",
        "status": "completed",
    })

    return _ok({
        "vendors": vendors,
        "total_amount": total_amount,
        "count": len(vendors),
    })


async def _get_account_balance(_args: dict) -> list[TextContent]:
    # Try Pine Labs Payouts balance API if available
    if hasattr(_pine, "get_account_balance"):
        try:
            run = await _db.get_run(RUN_ID) if RUN_ID else None
            token = run["pine_token"] if run and run.get("pine_token") else None
            if token:
                result = await _pine.get_account_balance(token)
                balance = float(result.get("balance", result.get("available_balance", 0)))
                return _ok({
                    "balance": balance,
                    "currency": result.get("currency", "INR"),
                    "sufficient": balance > 0,
                    "source": "pine_labs_payouts",
                })
        except Exception:
            pass  # Fall through to mock balance
    return _ok({
        "balance": 10_00_000.0,  # Rs 10 lakh mock balance
        "currency": "INR",
        "sufficient": True,
        "source": "mock",
    })


# --- SCORING & GATES ---

async def _score_vendors_tool(args: dict) -> list[TextContent]:
    run_id: str = args["run_id"]
    persona: str = args["persona"]

    persona_config = _load_persona(persona)

    # Get vendors for this run from CSV (source of truth for amounts)
    run = await _db.get_run(run_id)
    csv_path = run.get("invoice_source", "") if run else ""

    # Read CSV to get full vendor data including amounts
    csv_vendors: list[dict] = []
    if csv_path:
        try:
            with open(csv_path, newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    csv_vendors.append({
                        "id": row.get("vendor_id", ""),
                        "name": row.get("vendor_name", ""),
                        "category": row.get("category", ""),
                        "amount": float(row.get("amount", 0) or 0),
                        "preferred_rail": row.get("preferred_rail", "neft"),
                        "vendor_type": row.get("vendor_type", ""),
                        "drug_schedule": row.get("drug_schedule", "") or "",
                        "credit_days": int(row.get("credit_days", 0) or 0),
                        "invoice_date": row.get("invoice_date", ""),
                        "due_date": row.get("due_date", ""),
                    })
        except Exception:
            pass

    # Fallback to DB if CSV read fails
    if not csv_vendors:
        csv_vendors = await _db.get_vendors_by_persona(persona)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    ranked = _score_vendors(csv_vendors, persona_config, today)

    # Persist scores back to vendors table
    for v in ranked:
        try:
            await _db._db.execute(
                "UPDATE vendors SET priority_score = ?, priority_reason = ?, action = ? WHERE id = ?",
                (v.get("priority_score", 0), v.get("priority_reason", ""), v.get("action", "pay"), v["id"]),
            )
        except Exception:
            pass
    await _db._db.commit()

    await _emit_event("PIPELINE_STEP", {
        "run_id": run_id,
        "step": "SCORE",
        "status": "completed",
    })

    # Emit updated VENDOR_STATE with scores and amounts
    await _emit_event("VENDOR_STATE", {
        "run_id": run_id,
        "vendors": [
            {
                "vendor_id": v["id"],
                "name": v["name"],
                "amount": v.get("amount", 0),
                "rail": v.get("preferred_rail", "neft"),
                "state": "CREATED",
                "priority_score": v.get("priority_score", 0),
                "priority_reason": v.get("priority_reason", ""),
                "action": v.get("action", "pay"),
            }
            for v in ranked
        ],
    })

    return _ok({"ranked_vendors": ranked})


async def _request_policy_approval(args: dict) -> list[TextContent]:
    run_id: str = args["run_id"]
    vendors_summary: str = args["vendors_summary"]

    # Read vendors from the run's CSV (source of truth for amounts)
    # Then merge with DB scores if available
    run = await _db.get_run(run_id)
    csv_path = run.get("invoice_source", "") if run else ""
    persona = run.get("persona", "hospital") if run else "hospital"

    # Build vendor list from CSV
    ranked_vendors = []
    total = 0.0
    if csv_path:
        try:
            with open(csv_path, newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    vid = row.get("vendor_id", "")
                    amount = float(row.get("amount", 0) or 0)
                    total += amount

                    # Try to get score from DB
                    db_vendor = await _db.get_vendor(vid)
                    priority_score = db_vendor.get("priority_score", 0) if db_vendor else 0
                    priority_reason = db_vendor.get("priority_reason", "") if db_vendor else ""
                    action = db_vendor.get("action", "pay") if db_vendor else "pay"

                    ranked_vendors.append({
                        "vendor_id": vid,
                        "name": row.get("vendor_name", ""),
                        "amount": amount,
                        "rail": row.get("preferred_rail", "neft"),
                        "priority_reason": priority_reason or row.get("drug_schedule", ""),
                        "action": action,
                        "priority_score": priority_score,
                        "items": int(row.get("items", 0) or 0),
                        "invoice_number": row.get("invoice_number", ""),
                        "due_date": row.get("due_date", ""),
                        "drug_schedule": row.get("drug_schedule", ""),
                    })
        except Exception:
            pass

    # Fallback: read from DB vendors table (which now has amounts)
    if not ranked_vendors:
        db_vendors = await _db.get_vendors_by_persona(persona)
        for v in db_vendors:
            amount = float(v.get("amount", 0) or 0)
            total += amount
            ranked_vendors.append({
                "vendor_id": v["id"],
                "name": v["name"],
                "amount": amount,
                "rail": v.get("preferred_rail", "neft"),
                "priority_reason": v.get("priority_reason", ""),
                "action": v.get("action", "pay"),
                "priority_score": v.get("priority_score", 0),
                "items": int(v.get("items", 0) or 0),
                "invoice_number": v.get("invoice_number", ""),
                "due_date": v.get("due_date", ""),
                "drug_schedule": v.get("drug_schedule", ""),
            })

    await _emit_event("POLICY_GATE", {
        "run_id": run_id,
        "vendors_summary": vendors_summary,
        "vendors": ranked_vendors,
        "total": total,
        "requested_at": _now_iso(),
    })
    await _emit_event("CANVAS_STATE", {
        "state": "policy_gate",
        "run_id": run_id,
    })

    await _emit_event("PIPELINE_STEP", {
        "run_id": run_id,
        "step": "APPROVE",
        "status": "started",
    })

    result = await _wait_for_approval("approve", run_id)

    # Handle full rejection
    if result.get("rejected"):
        await _db.update_run_status(run_id, "rejected")
        await _emit_event("PIPELINE_STEP", {
            "run_id": run_id,
            "step": "APPROVE",
            "status": "rejected",
        })
        return _err("Run rejected by operator")

    approved_at = result.get("approved_at", _now_iso())
    vendor_decisions: dict = result.get("vendor_decisions") or {}

    # Apply per-vendor decisions to the DB and update vendors_summary list
    if vendor_decisions:
        for vendor in ranked_vendors:
            vid = vendor["vendor_id"]
            decision = vendor_decisions.get(vid)
            if decision in ("reject", "defer"):
                vendor["action"] = decision
                db_vendor = await _db.get_vendor(vid)
                if db_vendor:
                    db_vendor["action"] = decision
                    await _db.upsert_vendor(db_vendor)

        # Emit updated CANVAS_STATE so the frontend reflects decisions
        await _emit_event("CANVAS_STATE", {
            "state": "approval_decisions",
            "run_id": run_id,
            "vendor_decisions": vendor_decisions,
            "vendors": ranked_vendors,
        })

    await _db.update_run_status(run_id, "approved", approved_at=approved_at)

    await _emit_event("PIPELINE_STEP", {
        "run_id": run_id,
        "step": "APPROVE",
        "status": "completed",
    })

    return _ok({
        "approved": True,
        "approved_at": approved_at,
        "vendor_decisions": vendor_decisions,
        "vendors_summary": ranked_vendors,
    })


async def _request_escalation_decision(args: dict) -> list[TextContent]:
    vendor_id: str = args["vendor_id"]
    flag_type: str = args["flag_type"]
    details: str = args["details"]

    # Use run_id:vendor_id as the gate key so frontend can resolve it
    gate_id = f"{RUN_ID}:{vendor_id}"

    await _emit_event("ESCALATION", {
        "vendor_id": vendor_id,
        "flag_type": flag_type,
        "details": details,
        "run_id": RUN_ID,
        "requested_at": _now_iso(),
    })

    result = await _wait_for_approval("escalation", gate_id)
    decision = result.get("decision", "cancel")

    return _ok({"decision": decision, "vendor_id": vendor_id})


# --- PINE LABS OPS ---

async def _create_batch_order(args: dict) -> list[TextContent]:
    """Create ONE Pine Labs order grouping ALL vendors in a run."""
    run_id: str = args["run_id"]
    pre_auth: bool = args.get("pre_auth", False)

    run = await _db.get_run(run_id)
    if not run:
        return _err(f"Run not found: {run_id}")
    token: str = run["pine_token"]
    csv_path = run.get("invoice_source", "")
    persona = run.get("persona", "hospital")

    # Read all vendors from CSV
    vendor_rows: list[dict] = []
    total_amount = 0.0
    if csv_path:
        with open(csv_path, newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                amount = float(row.get("amount", 0) or 0)
                total_amount += amount
                vendor_rows.append({
                    "vendor_id": row.get("vendor_id", ""),
                    "name": row.get("vendor_name", ""),
                    "amount": amount,
                    "preferred_rail": row.get("preferred_rail", "neft"),
                    "drug_schedule": row.get("drug_schedule", ""),
                })

    if not vendor_rows:
        return _err("No vendors found in CSV")

    # Create ONE Pine Labs order for the total
    mor = f"PRIYA-{run_id[:8]}-BATCH-{uuid.uuid4().hex[:6]}".upper()
    try:
        pine_resp = await _pine.create_order(
            token=token,
            merchant_order_reference=mor,
            amount_paisa=_rupees_to_paisa(total_amount),
            pre_auth=pre_auth,
        )
    except Exception as e:
        return _err(f"Pine Labs create_order failed: {e}")
    pine_order = pine_resp.get("data", pine_resp)
    pine_order_id = pine_order.get("order_id", "")

    # Create individual DB order records per vendor, all linked to same pine_order_id
    order_ids = []
    for v in vendor_rows:
        order_id = str(uuid.uuid4())
        order = {
            "id": order_id,
            "run_id": run_id,
            "vendor_id": v["vendor_id"],
            "pine_order_id": pine_order_id,
            "merchant_order_reference": mor,
            "amount": v["amount"],
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
        order_ids.append(order_id)

    await _emit_event("PIPELINE_STEP", {
        "run_id": run_id,
        "step": "ORDER",
        "status": "completed",
    })

    # Emit VENDOR_STATE for all vendors at once
    await _emit_event("VENDOR_STATE", {
        "run_id": run_id,
        "vendors": [
            {
                "vendor_id": v["vendor_id"],
                "name": v["name"],
                "amount": v["amount"],
                "rail": v["preferred_rail"],
                "state": "AUTHORIZED",
                "pine_order_id": pine_order_id,
            }
            for v in vendor_rows
        ],
    })

    await _emit_event("CANVAS_STATE", {
        "state": "paying",
        "run_id": run_id,
    })

    return _ok({
        "pine_order_id": pine_order_id,
        "order_ids": order_ids,
        "total_amount": total_amount,
        "vendor_count": len(vendor_rows),
        "status": pine_order.get("status", "CREATED"),
        "mor": mor,
    })


async def _create_batch_payment(args: dict) -> list[TextContent]:
    """Execute ONE payment for the batch order covering all vendors."""
    run_id: str = args["run_id"]
    rail: str = args["rail"]

    run = await _db.get_run(run_id)
    if not run:
        return _err(f"Run not found: {run_id}")
    token: str = run["pine_token"]

    # Get all orders for this run — they should share the same pine_order_id
    orders = await _db.get_orders_by_run(run_id)
    if not orders:
        return _err("No orders found for this run")

    pine_order_id = orders[0]["pine_order_id"]
    total_amount = sum(o["amount"] for o in orders)

    # Count existing payment attempts
    existing_payments = await _db.get_payments_by_order(orders[0]["id"])
    attempt_number = len(existing_payments) + 1

    mpr = f"PAY-{run_id[:8]}-BATCH-{attempt_number}-{uuid.uuid4().hex[:4]}".upper()

    pine_resp = await _build_payment_via_pine(
        token=token,
        pine_order_id=pine_order_id,
        rail=rail,
        amount_paisa=_rupees_to_paisa(total_amount),
        merchant_payment_reference=mpr,
    )
    pine_payment = pine_resp.get("data", pine_resp)
    pine_payment_id = pine_payment.get("payment_id", pine_payment.get("order_id", ""))
    status = pine_payment.get("status", "PROCESSED")
    # Extract payment link URL — check both wrapper and top-level
    payment_link_url = (
        pine_payment.get("payment_link", "")
        or pine_resp.get("payment_link", "")
        or pine_resp.get("data", {}).get("payment_link", "")
    )

    # Create a payment record for EACH order in the batch
    payment_id = None  # will hold the last inserted payment id for the result
    for o in orders:
        pid = str(uuid.uuid4())
        if payment_id is None:
            payment_id = pid  # use first as representative id in result
        payment = {
            "id": pid,
            "run_id": run_id,
            "order_id": o["id"],
            "vendor_id": o["vendor_id"],
            "pine_order_id": pine_order_id,
            "pine_payment_id": pine_payment_id,
            "merchant_payment_reference": mpr,
            "amount": o["amount"],
            "rail": rail,
            "attempt_number": attempt_number,
            "pine_status": status,
            "failure_reason": pine_payment.get("failure_reason"),
            "recovery_action": payment_link_url or None,
            "webhook_event": None,
            "request_id": pine_payment.get("payment_id", ""),
            "initiated_at": _now_iso(),
            "confirmed_at": _now_iso() if status in ("PROCESSED", "SUCCESS") else None,
        }
        await _db.insert_payment(payment)

    # Determine the vendor display state
    is_confirmed = status in ("PROCESSED", "SUCCESS")
    is_awaiting = status == "AWAITING_PAYMENT"

    # Update all order statuses
    vendor_states = []
    if is_confirmed:
        for o in orders:
            await _db.update_order_status(o["id"], "PROCESSED")
            vendor = await _db.get_vendor(o["vendor_id"])
            vendor_states.append({
                "vendor_id": o["vendor_id"],
                "name": vendor.get("name", "") if vendor else "",
                "amount": o["amount"],
                "rail": rail,
                "state": "PROCESSED",
                "pine_order_id": pine_order_id,
                "payment_link": payment_link_url,
            })
    elif is_awaiting:
        for o in orders:
            await _db.update_order_status(o["id"], "AWAITING_PAYMENT")
            vendor = await _db.get_vendor(o["vendor_id"])
            vendor_states.append({
                "vendor_id": o["vendor_id"],
                "name": vendor.get("name", "") if vendor else "",
                "amount": o["amount"],
                "rail": rail,
                "state": "PENDING",
                "pine_order_id": pine_order_id,
                "payment_link": payment_link_url,
            })
    else:
        for o in orders:
            vendor = await _db.get_vendor(o["vendor_id"])
            vendor_states.append({
                "vendor_id": o["vendor_id"],
                "name": vendor.get("name", "") if vendor else "",
                "amount": o["amount"],
                "rail": rail,
                "state": "FAILED",
                "pine_order_id": pine_order_id,
            })

    pay_step_status = "completed" if is_confirmed else ("awaiting_webhook" if is_awaiting else "failed")
    await _emit_event("PIPELINE_STEP", {
        "run_id": run_id,
        "step": "PAY",
        "status": pay_step_status,
    })

    await _emit_event("VENDOR_STATE", {
        "run_id": run_id,
        "vendors": vendor_states,
    })

    result = {
        "pine_payment_id": pine_payment_id,
        "payment_id": payment_id,
        "status": status,
        "total_amount": total_amount,
        "vendor_count": len(orders),
        "attempt_number": attempt_number,
        "rail": rail,
        "payment_link": payment_link_url,
    }

    if is_awaiting:
        result["IMPORTANT"] = (
            "Payment link has been GENERATED but NOT YET PAID. "
            "You MUST call await_payment_confirmation(run_id) NOW and wait for the "
            "Pine Labs webhook (ORDER_PROCESSED) before proceeding to settlements. "
            "Do NOT call run_settlements until payment is confirmed."
        )

    return _ok(result)


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
        "step": "ORDER",
        "run_id": run_id,
        "vendor_id": vendor_id,
        "status": "completed",
    })
    await _emit_event("VENDOR_STATE", {
        "vendor_id": vendor_id,
        "name": vendor.get("name", ""),
        "amount": amount,
        "rail": vendor.get("preferred_rail", "neft"),
        "state": "AUTHORIZED",
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
    """Route to the correct pine_client method based on rail.

    Pine Labs UAT doesn't support seamless UPI/Netbanking payments for this
    merchant, so NEFT/UPI fall back to payment_link which works reliably.
    """
    match rail:
        case "payment_link" | "neft" | "upi" | "upi_intent":
            # Payment link is the reliable UAT rail — use for all B2B payments
            resp = await _pine.create_payment_link(
                token, amount_paisa, merchant_payment_reference,
                description=f"PRIYA vendor payment {merchant_payment_reference}",
            )
            link_url = resp.get("payment_link", "")
            import sys
            print(f"[PRIYA DEBUG] create_payment_link response keys: {list(resp.keys())}, payment_link={link_url!r}", file=sys.stderr, flush=True)
            return {
                "data": {
                    "payment_id": resp.get("payment_link_id", ""),
                    "order_id": resp.get("order_id", pine_order_id),
                    "status": "AWAITING_PAYMENT",
                    "payment_link": link_url,
                    "payment_method": "PAYMENT_LINK",
                }
            }
        case "hosted_checkout":
            run_resp = await _pine.get_order(token, pine_order_id)
            order_data = run_resp.get("data", run_resp)
            mor = order_data.get("merchant_order_reference", merchant_payment_reference)
            resp = await _pine.hosted_checkout(
                token, mor, amount_paisa,
                callback_url="http://localhost:8000/webhook/checkout",
            )
            return {
                "data": {
                    "payment_id": resp.get("order_id", ""),
                    "order_id": resp.get("order_id", pine_order_id),
                    "status": "AWAITING_PAYMENT",
                    "redirect_url": resp.get("redirect_url", ""),
                    "payment_method": "HOSTED_CHECKOUT",
                }
            }
        case "upi_collect":
            return await _pine.create_payment_upi_collect(
                token, pine_order_id, merchant_payment_reference, amount_paisa,
                payer_vpa=payer_vpa or "",
            )
        case "netbanking":
            return await _pine.create_payment_netbanking(
                token, pine_order_id, merchant_payment_reference, amount_paisa
            )
        case _:
            # Default to payment link (most reliable on UAT)
            resp = await _pine.create_payment_link(
                token, amount_paisa, merchant_payment_reference,
                description=f"PRIYA vendor payment {merchant_payment_reference}",
            )
            return {
                "data": {
                    "payment_id": resp.get("payment_link_id", ""),
                    "order_id": resp.get("order_id", pine_order_id),
                    "status": "AWAITING_PAYMENT",
                    "payment_link": resp.get("payment_link", ""),
                    "payment_method": "PAYMENT_LINK",
                }
            }


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
    payment_link_url = (
        pine_payment.get("payment_link", "")
        or pine_resp.get("payment_link", "")
    )

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
        "recovery_action": payment_link_url or None,
        "webhook_event": None,
        "request_id": pine_payment.get("payment_id", ""),
        "initiated_at": _now_iso(),
        "confirmed_at": _now_iso() if status in ("PROCESSED", "SUCCESS") else None,
    }
    await _db.insert_payment(payment)

    if status in ("PROCESSED", "SUCCESS"):
        await _db.update_order_status(order_id, "PROCESSED")
    elif status == "AWAITING_PAYMENT":
        await _db.update_order_status(order_id, "AWAITING_PAYMENT")

    is_awaiting = status == "AWAITING_PAYMENT"
    pay_status = "completed" if status in ("PROCESSED", "SUCCESS") else ("awaiting_webhook" if is_awaiting else "failed")

    await _emit_event("PIPELINE_STEP", {
        "run_id": order["run_id"],
        "step": "PAY",
        "vendor_id": order["vendor_id"],
        "status": pay_status,
    })
    await _emit_event("VENDOR_STATE", {
        "vendor_id": order["vendor_id"],
        "name": (await _db.get_vendor(order["vendor_id"]) or {}).get("name", ""),
        "amount": amount,
        "rail": rail,
        "state": "PENDING" if is_awaiting else ("PROCESSED" if status in ("PROCESSED", "SUCCESS") else "FAILED"),
        "pine_payment_id": pine_payment_id,
        "payment_link": payment_link_url,
        "attempt_number": attempt_number,
    })

    result = {
        "pine_payment_id": pine_payment_id,
        "payment_id": payment_id,
        "status": status,
        "attempt_number": attempt_number,
        "payment_link": payment_link_url,
    }
    if is_awaiting and payment_link_url:
        result["IMPORTANT"] = (
            "Payment link created. The owner must pay via this link. "
            "Call await_payment_confirmation(run_id) to wait for payment."
        )
    return _ok(result)


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


async def _await_payment_confirmation(args: dict) -> list[TextContent]:
    """
    Poll the DB for payment status changes after payment links are generated.
    Emits PAYMENT_AWAITING canvas state so frontend shows a waiting UI.
    Returns when all payments are confirmed or timeout.
    """
    run_id: str = args["run_id"]
    timeout = int(args.get("timeout_seconds", 600))
    poll_interval = int(args.get("poll_interval_seconds", 10))

    orders = await _db.get_orders_by_run(run_id)
    if not orders:
        return _err("No orders found for this run")

    # Gather initial payment state
    total_payments = 0
    payment_links = []
    payment_link_url = ""
    for order in orders:
        payments = await _db.get_payments_by_order(order["id"])
        for p in payments:
            total_payments += 1
            if p.get("recovery_action"):
                payment_link_url = p["recovery_action"]
            if p.get("pine_status") not in ("PROCESSED", "SUCCESS", "FAILED", "CANCELLED"):
                payment_links.append({
                    "payment_id": p["id"],
                    "pine_order_id": p.get("pine_order_id", ""),
                    "amount": p.get("amount", 0),
                    "status": p.get("pine_status", "PENDING"),
                    "payment_link": p.get("recovery_action", ""),
                })

    # Emit waiting state to frontend
    await _emit_event("CANVAS_STATE", {
        "state": "awaiting_payment",
        "run_id": run_id,
    })

    await _emit_event("PAYMENT_AWAITING", {
        "run_id": run_id,
        "total_payments": total_payments,
        "pending_count": len(payment_links),
        "timeout_seconds": timeout,
        "payment_link": payment_link_url,
        "message": f"Waiting for {len(payment_links)} payment(s) to be confirmed via webhook...",
    })

    # Poll loop
    elapsed = 0
    while elapsed < timeout:
        await asyncio.sleep(poll_interval)
        elapsed += poll_interval

        # Re-check all payment statuses
        pending = []
        confirmed = []
        failed = []

        for order in orders:
            payments = await _db.get_payments_by_order(order["id"])
            for p in payments:
                status = p.get("pine_status", "PENDING")
                if status in ("PROCESSED", "SUCCESS"):
                    confirmed.append(p)
                elif status in ("FAILED", "CANCELLED"):
                    failed.append(p)
                else:
                    pending.append(p)

        # Broadcast progress update
        await _emit_event("PAYMENT_AWAITING", {
            "run_id": run_id,
            "total_payments": total_payments,
            "confirmed_count": len(confirmed),
            "failed_count": len(failed),
            "pending_count": len(pending),
            "elapsed_seconds": elapsed,
            "timeout_seconds": timeout,
            "message": (
                f"{len(confirmed)} confirmed, {len(failed)} failed, "
                f"{len(pending)} pending — {elapsed}s elapsed"
            ),
        })

        # All resolved?
        if not pending:
            break

    # Final status
    final_confirmed = []
    final_failed = []
    final_pending = []
    for order in orders:
        payments = await _db.get_payments_by_order(order["id"])
        for p in payments:
            status = p.get("pine_status", "PENDING")
            if status in ("PROCESSED", "SUCCESS"):
                final_confirmed.append(p)
            elif status in ("FAILED", "CANCELLED"):
                final_failed.append(p)
            else:
                final_pending.append(p)

    timed_out = len(final_pending) > 0

    # Reset canvas state to paying (done waiting)
    await _emit_event("CANVAS_STATE", {
        "state": "paying",
        "run_id": run_id,
    })

    await _emit_event("AGENT_NARRATION", {
        "run_id": run_id,
        "message": (
            f"Payment confirmation: {len(final_confirmed)} confirmed, "
            f"{len(final_failed)} failed"
            + (f", {len(final_pending)} still pending (timed out after {timeout}s)" if timed_out else "")
        ),
        "level": "warn" if timed_out or final_failed else "info",
    })

    return _ok({
        "confirmed_count": len(final_confirmed),
        "failed_count": len(final_failed),
        "pending_count": len(final_pending),
        "timed_out": timed_out,
        "elapsed_seconds": elapsed,
        "confirmed_payments": [
            {"id": p["id"], "amount": p["amount"], "status": p["pine_status"]}
            for p in final_confirmed
        ],
        "failed_payments": [
            {"id": p["id"], "amount": p["amount"], "status": p["pine_status"],
             "reason": p.get("failure_reason", "")}
            for p in final_failed
        ],
    })


# --- PAYOUTS (Pine Labs Payouts API v3) ---

async def _execute_payouts(args: dict) -> list[TextContent]:
    """Execute payouts for all approved vendors in a run via Pine Labs Payouts API."""
    run_id: str = args["run_id"]
    mode: str = args.get("mode", "IMPS").upper()

    run = await _db.get_run(run_id)
    if not run:
        return _err(f"Run not found: {run_id}")
    token: str = run["pine_token"]

    # Get all orders for this run (created in the ORDER step)
    orders = await _db.get_orders_by_run(run_id)
    if not orders:
        return _err("No orders found for this run")

    sim_label = " (simulated — UAT merchant not enabled for payouts)" if PINE_PAYOUT_SIM else ""
    await _emit_event("AGENT_NARRATION", {
        "run_id": run_id,
        "message": f"Executing payouts for {len(orders)} vendor(s) via {mode}{sim_label}...",
        "level": "info",
    })

    scheduled = 0
    failed = 0
    payouts_summary: list[dict] = []

    for order in orders:
        # Skip deferred vendors
        if order.get("action") == "defer":
            continue

        vendor_id = order["vendor_id"]
        vendor = await _db.get_vendor(vendor_id)
        if not vendor:
            failed += 1
            payouts_summary.append({
                "vendor_id": vendor_id,
                "status": "FAILED",
                "reason": "Vendor not found in DB",
            })
            continue

        payee_name = vendor.get("payee_name") or vendor.get("name", "")
        account_number = vendor.get("bank_account", "")
        branch_code = vendor.get("ifsc", "")
        email = vendor.get("email", "")
        phone = vendor.get("phone", "")
        upi_id = vendor.get("upi_id", "")
        amount_rupees = order["amount"]

        # Generate unique client reference ID
        client_ref = f"PRIYA-{run_id[:8]}-{vendor_id[:8]}-{uuid.uuid4().hex[:6]}".upper()

        try:
            if PINE_PAYOUT_SIM:
                # Simulate payout — merchant not enabled for payouts on UAT
                pine_resp = {
                    "clientReferenceId": client_ref,
                    "paymentReferenceId": f"SIM-PAY-{uuid.uuid4().hex[:8].upper()}",
                    "payeeName": payee_name,
                    "accountNumber": account_number,
                    "branchCode": branch_code,
                    "amount": {"currency": "INR", "value": amount_rupees},
                    "mode": mode,
                    "status": "SCHEDULED",
                    "message": "Payout scheduled (simulated — UAT merchant not enabled for payouts)",
                    "scheduledAt": _now_iso(),
                    "remarks": f"Vendor payment via PRIYA — {order.get('merchant_order_reference', '')}",
                }
            else:
                pine_resp = await _pine.create_payout(
                    token=token,
                    client_reference_id=client_ref,
                    payee_name=payee_name,
                    account_number=account_number,
                    branch_code=branch_code,
                    amount_rupees=amount_rupees,
                    mode=mode,
                    email=email,
                    phone=phone,
                    remarks=f"Vendor payment via PRIYA — {order.get('merchant_order_reference', '')}",
                )

            payout_status = pine_resp.get("status", "SCHEDULED")
            payout_id = pine_resp.get("id", pine_resp.get("paymentReferenceId", pine_resp.get("payout_id", client_ref)))

            # Insert payout record in DB
            payout_record = {
                "id": str(uuid.uuid4()),
                "run_id": run_id,
                "order_id": order["id"],
                "vendor_id": vendor_id,
                "client_reference_id": client_ref,
                "payout_id": payout_id,
                "payee_name": payee_name,
                "account_number": account_number,
                "branch_code": branch_code,
                "amount": amount_rupees,
                "mode": mode,
                "status": payout_status,
                "attempt_number": 1,
                "failure_reason": None,
                "bank_txn_reference_id": pine_resp.get("bankTxnReferenceId"),
                "created_at": _now_iso(),
                "updated_at": _now_iso(),
            }
            await _db.insert_payout(payout_record)

            scheduled += 1
            payouts_summary.append({
                "vendor_id": vendor_id,
                "vendor_name": payee_name,
                "amount": amount_rupees,
                "client_reference_id": client_ref,
                "payout_id": payout_id,
                "status": payout_status,
                "mode": mode,
            })

            # Emit vendor state based on payout status
            state = "PROCESSING" if payout_status in ("SCHEDULED", "PENDING", "PROCESSING") else (
                "PROCESSED" if payout_status in ("PROCESSED", "SUCCESS") else "FAILED"
            )
            await _emit_event("VENDOR_STATE", {
                "vendor_id": vendor_id,
                "name": payee_name,
                "amount": amount_rupees,
                "rail": f"payout_{mode.lower()}",
                "state": state,
                "payout_id": payout_id,
                "client_reference_id": client_ref,
            })

        except Exception as e:
            failed += 1
            payouts_summary.append({
                "vendor_id": vendor_id,
                "vendor_name": payee_name,
                "amount": amount_rupees,
                "status": "FAILED",
                "reason": str(e),
                "mode": mode,
            })

            await _emit_event("VENDOR_STATE", {
                "vendor_id": vendor_id,
                "name": payee_name,
                "amount": amount_rupees,
                "rail": f"payout_{mode.lower()}",
                "state": "FAILED",
                "reason": str(e),
            })

    # Emit pipeline step
    pay_status = "completed" if failed == 0 else ("partial" if scheduled > 0 else "failed")
    await _emit_event("PIPELINE_STEP", {
        "run_id": run_id,
        "step": "PAY",
        "status": pay_status,
        "method": "payouts_api",
    })

    return _ok({
        "total": scheduled + failed,
        "scheduled": scheduled,
        "failed": failed,
        "mode": mode,
        "payouts": payouts_summary,
    })


async def _poll_payout_status(args: dict) -> list[TextContent]:
    """Poll Pine Labs for payout status updates on all pending payouts in a run."""
    run_id: str = args["run_id"]
    timeout = int(args.get("timeout_seconds", 120))
    poll_interval = int(args.get("poll_interval_seconds", 5))

    run = await _db.get_run(run_id)
    if not run:
        return _err(f"Run not found: {run_id}")
    token: str = run["pine_token"]

    terminal_statuses = {"SUCCESS", "FAILED", "REVERSED", "CANCELLED"}

    # Emit waiting state
    await _emit_event("CANVAS_STATE", {
        "state": "awaiting_payment",
        "run_id": run_id,
    })

    elapsed = 0
    while elapsed < timeout:
        # Get all payouts for this run that are not yet terminal
        all_payouts = await _db.get_payouts_by_run(run_id)
        pending = [p for p in all_payouts if p.get("status", "").upper() not in terminal_statuses]

        if not pending:
            break

        # Poll each pending payout
        for payout in pending:
            client_ref = payout.get("client_reference_id", "")
            if not client_ref:
                continue

            try:
                if PINE_PAYOUT_SIM:
                    # Simulate status progression: SCHEDULED → SUCCESS after ~5s
                    cur = payout.get("status", "SCHEDULED").upper()
                    if cur in ("SCHEDULED", "PENDING", "PROCESSING") and elapsed >= 5:
                        new_status = "SUCCESS"
                        utr = f"SIM{uuid.uuid4().hex[:12].upper()}"
                    else:
                        new_status = cur
                        utr = payout.get("bank_txn_reference_id")
                else:
                    resp = await _pine.get_payout_status(token, client_ref)
                    new_status = resp.get("status", payout["status"]).upper()
                    utr = resp.get("bankTxnReferenceId") or payout.get("bank_txn_reference_id")

                if new_status != payout["status"]:
                    await _db.update_payout_status(
                        payout["id"],
                        new_status,
                        bank_txn_reference_id=utr,
                        updated_at=_now_iso(),
                    )

                    # Emit vendor state update
                    state = "PROCESSED" if new_status == "SUCCESS" else (
                        "FAILED" if new_status in ("FAILED", "REVERSED", "CANCELLED") else "PROCESSING"
                    )
                    await _emit_event("VENDOR_STATE", {
                        "vendor_id": payout["vendor_id"],
                        "name": payout.get("payee_name", ""),
                        "amount": payout.get("amount", 0),
                        "rail": f"payout_{payout.get('mode', 'IMPS').lower()}",
                        "state": state,
                        "payout_id": payout.get("payout_id", ""),
                        "utr_number": utr,
                    })
            except Exception:
                pass  # Will retry on next poll cycle

        # Emit progress
        all_payouts = await _db.get_payouts_by_run(run_id)
        success_count = sum(1 for p in all_payouts if p.get("status", "").upper() == "SUCCESS")
        failed_count = sum(1 for p in all_payouts if p.get("status", "").upper() in ("FAILED", "REVERSED", "CANCELLED"))
        pending_count = len(all_payouts) - success_count - failed_count

        await _emit_event("PAYMENT_AWAITING", {
            "run_id": run_id,
            "total_payments": len(all_payouts),
            "confirmed_count": success_count,
            "failed_count": failed_count,
            "pending_count": pending_count,
            "elapsed_seconds": elapsed,
            "timeout_seconds": timeout,
            "message": (
                f"{success_count} success, {failed_count} failed, "
                f"{pending_count} pending — {elapsed}s elapsed"
            ),
        })

        if pending_count == 0:
            break

        await asyncio.sleep(poll_interval)
        elapsed += poll_interval

    # Final tally
    all_payouts = await _db.get_payouts_by_run(run_id)
    success_list = [p for p in all_payouts if p.get("status", "").upper() == "SUCCESS"]
    failed_list = [p for p in all_payouts if p.get("status", "").upper() in ("FAILED", "REVERSED", "CANCELLED")]
    pending_list = [p for p in all_payouts if p.get("status", "").upper() not in terminal_statuses]
    timed_out = len(pending_list) > 0

    # Reset canvas state
    await _emit_event("CANVAS_STATE", {
        "state": "paying",
        "run_id": run_id,
    })

    await _emit_event("AGENT_NARRATION", {
        "run_id": run_id,
        "message": (
            f"Payout status: {len(success_list)} success, "
            f"{len(failed_list)} failed"
            + (f", {len(pending_list)} still pending (timed out after {timeout}s)" if timed_out else "")
        ),
        "level": "warn" if timed_out or failed_list else "info",
    })

    return _ok({
        "success_count": len(success_list),
        "failed_count": len(failed_list),
        "pending_count": len(pending_list),
        "timed_out": timed_out,
        "elapsed_seconds": elapsed,
        "success_payouts": [
            {"id": p["id"], "vendor_id": p["vendor_id"], "amount": p["amount"],
             "utr_number": p.get("bank_txn_reference_id", "")}
            for p in success_list
        ],
        "failed_payouts": [
            {"id": p["id"], "vendor_id": p["vendor_id"], "amount": p["amount"],
             "reason": p.get("failure_reason", "")}
            for p in failed_list
        ],
    })


async def _retry_failed_payout(args: dict) -> list[TextContent]:
    """Retry a failed payout for a specific vendor with a different rail/mode."""
    run_id: str = args["run_id"]
    vendor_id: str = args["vendor_id"]
    new_mode: str = args["new_mode"].upper()

    run = await _db.get_run(run_id)
    if not run:
        return _err(f"Run not found: {run_id}")
    token: str = run["pine_token"]

    # Find the failed payout for this vendor
    vendor_payouts = await _db.get_payouts_by_vendor(run_id, vendor_id)
    failed_payout = None
    for p in reversed(vendor_payouts):  # Most recent first
        if p.get("status", "").upper() in ("FAILED", "REVERSED", "CANCELLED"):
            failed_payout = p
            break

    if not failed_payout:
        return _err(f"No failed payout found for vendor {vendor_id} in run {run_id}")

    vendor = await _db.get_vendor(vendor_id)
    if not vendor:
        return _err(f"Vendor not found: {vendor_id}")

    payee_name = vendor.get("payee_name") or vendor.get("name", "")
    account_number = vendor.get("bank_account", "")
    branch_code = vendor.get("ifsc", "")
    email = vendor.get("email", "")
    phone = vendor.get("phone", "")
    upi_id = vendor.get("upi_id", "")
    amount_rupees = failed_payout["amount"]
    attempt_number = failed_payout.get("attempt_number", 1) + 1

    # Generate new client reference
    client_ref = f"PRIYA-{run_id[:8]}-{vendor_id[:8]}-{uuid.uuid4().hex[:6]}".upper()

    # Emit rail switch event
    await _emit_event("RAIL_SWITCH", {
        "vendor_id": vendor_id,
        "failed_rail": f"payout_{failed_payout.get('mode', 'IMPS').lower()}",
        "new_rail": f"payout_{new_mode.lower()}",
        "failure_reason": failed_payout.get("failure_reason", "unknown"),
    })

    try:
        # If UPI mode and vendor has upi_id, the pine_client will handle it
        payout_account = account_number
        payout_branch = branch_code
        if new_mode == "UPI" and upi_id:
            # For UPI payouts, pine_client uses upi_id instead of bank details
            payout_account = upi_id
            payout_branch = ""

        if PINE_PAYOUT_SIM:
            pine_resp = {
                "clientReferenceId": client_ref,
                "paymentReferenceId": f"SIM-PAY-{uuid.uuid4().hex[:8].upper()}",
                "payeeName": payee_name,
                "accountNumber": payout_account,
                "branchCode": payout_branch,
                "amount": {"currency": "INR", "value": amount_rupees},
                "mode": new_mode,
                "status": "SCHEDULED",
                "message": f"Retry #{attempt_number} (simulated)",
                "scheduledAt": _now_iso(),
            }
        else:
            pine_resp = await _pine.create_payout(
                token=token,
                client_reference_id=client_ref,
                payee_name=payee_name,
                account_number=payout_account,
                branch_code=payout_branch,
                amount_rupees=amount_rupees,
                mode=new_mode,
                email=email,
                phone=phone,
                remarks=f"Retry #{attempt_number} — vendor payment via PRIYA",
            )

        payout_status = pine_resp.get("status", "SCHEDULED")
        payout_id = pine_resp.get("id", pine_resp.get("paymentReferenceId", pine_resp.get("payout_id", client_ref)))

        # Insert new payout record
        payout_record = {
            "id": str(uuid.uuid4()),
            "run_id": run_id,
            "order_id": failed_payout.get("order_id", ""),
            "vendor_id": vendor_id,
            "client_reference_id": client_ref,
            "payout_id": payout_id,
            "payee_name": payee_name,
            "account_number": payout_account,
            "branch_code": payout_branch,
            "amount": amount_rupees,
            "mode": new_mode,
            "status": payout_status,
            "attempt_number": attempt_number,
            "failure_reason": None,
            "bank_txn_reference_id": pine_resp.get("bankTxnReferenceId"),
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
        }
        await _db.insert_payout(payout_record)

        state = "PROCESSING" if payout_status in ("SCHEDULED", "PENDING", "PROCESSING") else (
            "PROCESSED" if payout_status in ("PROCESSED", "SUCCESS") else "FAILED"
        )
        await _emit_event("VENDOR_STATE", {
            "vendor_id": vendor_id,
            "name": payee_name,
            "amount": amount_rupees,
            "rail": f"payout_{new_mode.lower()}",
            "state": state,
            "payout_id": payout_id,
            "client_reference_id": client_ref,
            "attempt_number": attempt_number,
        })

        return _ok({
            "vendor_id": vendor_id,
            "client_reference_id": client_ref,
            "payout_id": payout_id,
            "status": payout_status,
            "mode": new_mode,
            "attempt_number": attempt_number,
            "amount": amount_rupees,
        })

    except Exception as e:
        return _err(f"Retry payout failed for vendor {vendor_id}: {e}")


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
    pine_order_ids = {o["pine_order_id"] for o in orders if o.get("pine_order_id")}

    # -----------------------------------------------------------------------
    # Pine Labs settlements API.
    #
    # Correct date format (confirmed via MCP docs + live test 2026-03-14):
    #   YYYY-MM-DDTHH:MM:SS  — no timezone suffix (Z or +05:30 → INVALID_DATE)
    # pine_client.get_all_settlements() auto-normalises plain YYYY-MM-DD.
    #
    # Live response schema (from MCP pinelabs-mcp-server v7.0.0 docs):
    #   data[].utr_number           — UTR string
    #   data[].total_amount         — net settled amount (rupees float)
    #   data[].actual_transaction_amount — gross transaction amount (rupees)
    #   data[].total_deduction_amount    — MDR + other deductions (rupees)
    #   data[].last_processed_date  — ISO datetime string
    #   data[].settled_date         — ISO datetime of settlement credit
    #   data[].programs             — list of payment rail strings
    #   data[].system               — "PG" etc.
    # -----------------------------------------------------------------------
    try:
        result = await _pine.get_all_settlements(token, start_date, end_date)
        raw_settlements = result.get("data", [])
    except Exception as e:
        import sys
        print(f"[SETTLEMENTS] Pine Labs API error (falling back to UAT synth): {e}", file=sys.stderr)
        raw_settlements = []

    matched_count = 0
    written: list[dict] = []
    uat_fallback = False

    if raw_settlements:
        # ---- Live Pine Labs data ----------------------------------------
        for s in raw_settlements:
            utr = s.get("utr_number", "")
            # amounts are in RUPEES (floats) in the live response
            total_amount = float(s.get("total_amount", 0))            # net settled
            actual_txn_amount = float(s.get("actual_transaction_amount", total_amount))  # gross
            total_deduction = float(s.get("total_deduction_amount", actual_txn_amount - total_amount))
            refund_debit = float(s.get("total_refund", 0))
            last_processed_date = s.get("last_processed_date", _now_iso())
            settled_at = s.get("settled_date", s.get("last_processed_date", _now_iso()))

            # Match to a specific order if possible via programs/orders list
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
                "bank_account": s.get("bank_acc_number", s.get("bank_account", "")),
                "last_processed_date": last_processed_date,
                "expected_amount": actual_txn_amount,
                "settled_amount": total_amount,
                "platform_fee": total_deduction - refund_debit,
                "total_deduction_amount": total_deduction,
                "refund_debit": refund_debit,
                "fee_flagged": False,
                "status": "SETTLED",
                "settled_at": settled_at,
                "created_at": _now_iso(),
            }
            # Only write settlements that match orders in this run to prevent
            # cross-run contamination. If no matched order, skip this settlement.
            if matched_pine_order_id is None:
                continue

            await _db.insert_settlement(settlement)
            written.append(settlement)
            matched_count += 1
    else:
        # ---- UAT FALLBACK ------------------------------------------------
        # Pine Labs UAT does not generate settlement records for test payments.
        # We synthesise realistic settlement entries so recon can run end-to-end.
        #
        # Real-world model: Pine Labs settles ONE amount per batch order (the
        # total collected minus MDR).  So we create ONE settlement per unique
        # pine_order_id, with expected_amount = sum of vendor amounts in that
        # batch.  This mirrors what a live settlement response looks like.
        #
        # UTR format:  AXISN<YYYYMMDD><4-digit-seq>   (realistic AXIS Bank UTR)
        # -----------------------------------------------------------------------
        uat_fallback = True
        persona_config = _load_persona(run.get("persona", "")) if run.get("persona") else {}
        MDR_RATE = persona_config.get("mdr_rate_contracted", 0.018)

        today_str = date.today().strftime("%Y%m%d")
        seq = 1

        # Group orders by pine_order_id (batch orders share the same one)
        batch_groups: dict[str, list[dict]] = {}
        for order in orders:
            pine_oid = order.get("pine_order_id", order["id"])
            batch_groups.setdefault(pine_oid, []).append(order)

        for pine_oid, batch_orders in batch_groups.items():
            # Sum confirmed amounts across all orders in the batch
            batch_total = 0.0
            latest_confirmed_at = None
            has_confirmed = False

            for order in batch_orders:
                payments = await _db.get_payments_by_order(order["id"])
                confirmed = [p for p in payments if p.get("pine_status") in ("PROCESSED", "SUCCESS")]
                if confirmed:
                    has_confirmed = True
                    payment = confirmed[-1]
                    batch_total += float(payment.get("amount", order.get("amount", 0)))
                    cat = payment.get("confirmed_at")
                    if cat and (latest_confirmed_at is None or cat > latest_confirmed_at):
                        latest_confirmed_at = cat

            if not has_confirmed:
                continue

            platform_fee = round(batch_total * MDR_RATE, 2)
            settled_amount = round(batch_total - platform_fee, 2)

            confirmed_at_raw = latest_confirmed_at or _now_iso()
            try:
                confirmed_dt = datetime.fromisoformat(confirmed_at_raw.replace("Z", "+00:00"))
                settled_dt = confirmed_dt + timedelta(days=1)
                settled_at = settled_dt.strftime("%Y-%m-%dT%H:%M:%S")
                last_processed_date = confirmed_at_raw
            except Exception:
                settled_at = _now_iso()
                last_processed_date = _now_iso()

            utr = f"AXISN{today_str}{seq:04d}"
            seq += 1

            settlement = {
                "id": str(uuid.uuid4()),
                "run_id": run_id,
                "pine_order_id": pine_oid,
                "pine_settlement_id": utr,
                "utr_number": utr,
                "bank_account": "UAT-FALLBACK",
                "last_processed_date": last_processed_date,
                "expected_amount": batch_total,
                "settled_amount": settled_amount,
                "platform_fee": platform_fee,
                "total_deduction_amount": platform_fee,
                "refund_debit": 0.0,
                "fee_flagged": False,
                "status": "SETTLED",
                "settled_at": settled_at,
                "created_at": _now_iso(),
            }
            await _db.insert_settlement(settlement)
            written.append(settlement)
            matched_count += 1

    await _emit_event("PIPELINE_STEP", {
        "run_id": run_id,
        "step": "SETTLE",
        "status": "completed",
    })
    await _emit_event("CANVAS_STATE", {
        "state": "settlement",
        "run_id": run_id,
    })

    return _ok({
        "settlements": written,
        "matched_count": matched_count,
        "uat_fallback": uat_fallback,
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

    # Index settlements by pine_order_id.
    # In the new model there is ONE settlement per batch pine_order_id.
    sett_by_pine_order: dict[str, dict] = {}
    for s in settlements:
        if s["pine_order_id"]:
            sett_by_pine_order[s["pine_order_id"]] = s

    # Build UTR -> [settlement, ...] index for duplicate UTR check (check 8)
    sett_by_utr: dict[str, list[dict]] = {}
    for s in settlements:
        utr = s.get("utr_number") or ""
        if utr:
            sett_by_utr.setdefault(utr, []).append(s)

    # Also fetch payouts for the new payout flow
    payouts = await _db.get_payouts_by_run(run_id)
    payout_by_vendor: dict[str, dict] = {}
    for p in payouts:
        vid = p.get("vendor_id", "")
        # Keep the most recent successful payout per vendor
        if vid and (vid not in payout_by_vendor or p.get("status") == "SUCCESS"):
            payout_by_vendor[vid] = p

    recons: list[dict] = []
    total_variance = 0.0
    mdr_drift_count = 0

    for order in orders:
        vendor_id = order["vendor_id"]
        invoice_amount = order["amount"]

        # Check for payout (new flow) first, then fall back to payment (old flow)
        payout = payout_by_vendor.get(vendor_id)
        payments = await _db.get_payments_by_order(order["id"])
        successful_payment = next(
            (p for p in payments if p["pine_status"] in ("PROCESSED", "SUCCESS")), None
        )
        # Look up the batch settlement for this order's pine_order_id.
        # In the new model, there is ONE settlement per batch with the total
        # amount.  Each vendor's share is computed proportionally.
        batch_sett = sett_by_pine_order.get(order["pine_order_id"])
        sett = batch_sett  # used for check functions that inspect settlement fields

        # Compute this vendor's proportional share of the batch settlement.
        # batch_total_orders = sum of all order amounts sharing this pine_order_id
        if batch_sett:
            batch_orders_for_pine = [
                o for o in orders
                if o.get("pine_order_id") == order["pine_order_id"]
            ]
            batch_total_orders = sum(float(o["amount"]) for o in batch_orders_for_pine)
            if batch_total_orders > 0:
                vendor_share = float(invoice_amount) / batch_total_orders
            else:
                vendor_share = 1.0 / max(len(batch_orders_for_pine), 1)
            vendor_settled = round(float(batch_sett["settled_amount"]) * vendor_share, 2)
            vendor_fee = round(float(batch_sett["platform_fee"]) * vendor_share, 2)
        else:
            vendor_settled = 0.0
            vendor_fee = 0.0

        # Determine paid amount and rail from payout or payment
        if payout and payout.get("status") == "SUCCESS":
            paid_amount = payout["amount"]
            rail_used = payout.get("mode", "IMPS")
            utr_number = payout.get("bank_txn_reference_id")
            retries = payout.get("attempt_number", 1)
            payment_id = payout["id"]  # Use payout ID as payment reference
            fees = float(payout.get("fees", 0) or 0)
        elif successful_payment:
            paid_amount = successful_payment["amount"]
            rail_used = successful_payment["rail"]
            utr_number = sett["utr_number"] if sett else None
            retries = len(payments)
            payment_id = successful_payment["id"]
            fees = vendor_fee  # vendor's proportional share of batch MDR
        else:
            paid_amount = 0.0
            rail_used = None
            utr_number = None
            retries = len(payments) + (payout.get("attempt_number", 0) if payout else 0)
            payment_id = payout["id"] if payout else None
            fees = 0.0

        # For payouts: settled = paid (direct bank transfer, no settlement delay)
        # For payments: settled = vendor's proportional share of batch settlement
        if payout and payout.get("status") == "SUCCESS":
            settled_amount = paid_amount  # Direct payout = settled immediately
        else:
            settled_amount = vendor_settled if sett else 0.0

        variance = round(invoice_amount - paid_amount, 2)
        total_variance += variance

        mdr_actual = 0.0
        mdr_drift_flagged = False
        if paid_amount > 0 and fees > 0:
            mdr_actual = round(fees / paid_amount, 6)
            if abs(mdr_actual - mdr_contracted) > 0.001:
                mdr_drift_flagged = True
                mdr_drift_count += 1

        # Determine outcome
        if payout and payout.get("status") == "SUCCESS":
            outcome = "matched" if variance == 0 else "variance"
        elif payout and payout.get("status") == "FAILED":
            outcome = "failed"
        elif payout and payout.get("status") in ("SCHEDULED", "PENDING", "PROCESSING"):
            outcome = "pending_payout"
        elif not successful_payment and not payout:
            outcome = "failed"
        elif not sett and not payout:
            outcome = "pending_settlement"
        elif variance != 0:
            outcome = "variance"
        else:
            outcome = "matched"

        # Settlements sharing the same UTR (for check 8)
        utr_key = utr_number or (sett["utr_number"] if sett else "")
        all_setts_for_utr = sett_by_utr.get(utr_key, []) if utr_key else []

        # Build partial recon dict for check runner
        partial_recon = {
            "paid_amount": paid_amount,
            "settled_amount": settled_amount,
            "vendor_fee": fees,  # vendor's proportional share of batch MDR
            "mdr_rate_actual": mdr_actual,
            "mdr_rate_contracted": mdr_contracted,
            "rail_used": rail_used,
            "bank_credit_amount": settled_amount if settled_amount > 0 else None,
            "invoice_amount": invoice_amount,
            "mdr_rates_by_rail": persona_config.get("mdr_rates_by_rail", {}),
            "payout": payout,  # vendor payout record (or None)
        }

        sett_missing = (sett is None) and (paid_amount > 0)
        checks, recon_status, delay_days = _run_recon_checks(
            partial_recon, sett, all_setts_for_utr, orders, sett_missing=sett_missing
        )

        # Override recon_status for successful payouts with no variance
        if payout and payout.get("status") == "SUCCESS" and variance == 0:
            recon_status = "MATCHED"
            checks = [{"check_id": 1, "name": "payout_confirmed", "severity": "info",
                        "passed": True, "detail": f"Payout SUCCESS via {rail_used}, UTR: {utr_number}"}]

        # Auto-populate ca_notes for mismatches
        ca_notes = None
        if recon_status == "MISMATCH":
            failed_checks = [c["name"] for c in checks if c.get("passed") is False]
            ca_notes = f"MISMATCH: invoice={invoice_amount}, paid={paid_amount}, settled={settled_amount}. Failed: {', '.join(failed_checks)}"
        elif outcome == "variance" and variance != 0:
            direction = "underpaid" if variance > 0 else "overpaid"
            ca_notes = f"Variance Rs {abs(variance):.2f} ({direction}). Invoice={invoice_amount}, Paid={paid_amount}"
        elif outcome == "pending_settlement":
            ca_notes = f"Settlement pending (T+1). Payment confirmed Rs {paid_amount} via {rail_used}"
        elif outcome == "failed":
            ca_notes = f"Payment failed. Invoice Rs {invoice_amount} for vendor {vendor_id}"

        recon = {
            "id": str(uuid.uuid4()),
            "run_id": run_id,
            "order_id": order["id"],
            "payment_id": payment_id,
            "settlement_id": sett["id"] if sett else None,
            "vendor_id": vendor_id,
            "pine_order_id": order["pine_order_id"],
            "merchant_order_reference": order["merchant_order_reference"],
            "utr_number": utr_number,
            "persona": persona,
            "invoice_amount": invoice_amount,
            "paid_amount": paid_amount,
            "settled_amount": settled_amount,
            "variance": variance,
            "mdr_rate_actual": mdr_actual,
            "mdr_rate_contracted": mdr_contracted,
            "mdr_drift_flagged": mdr_drift_flagged,
            "rail_used": rail_used,
            "retries": retries,
            "outcome": outcome,
            "pre_auth_used": order["pre_auth"],
            "agent_reasoning": order.get("priority_reason", ""),
            "ca_notes": ca_notes,
            "created_at": _now_iso(),
            "checks": json.dumps(checks),
            "recon_status": recon_status,
            "bank_credit_amount": settled_amount if settled_amount > 0 else None,
            "bank_delta": 0.0 if settled_amount > 0 else None,
            "settlement_delay_days": delay_days if not payout else 0,
            "fee_amount": fees,
            "settled_at_ts": sett.get("settled_at") if sett else None,
            "payout_id": payout["id"] if payout else None,
            "updated_at": _now_iso(),
        }
        await _db.insert_reconciliation(recon)
        recons.append(recon)

    summary = {
        "total_orders": len(orders),
        "matched": sum(1 for r in recons if r["recon_status"] == "MATCHED"),
        "mismatch_count": sum(1 for r in recons if r["recon_status"] == "MISMATCH"),
        "warning_count": sum(1 for r in recons if r["recon_status"] == "WARNING"),
        "pending_count": sum(1 for r in recons if r["recon_status"] == "PENDING"),
        "variance_count": sum(1 for r in recons if r["outcome"] == "variance"),
        "failed_count": sum(1 for r in recons if r["outcome"] == "failed"),
        "pending_settlement_count": sum(1 for r in recons if r["outcome"] == "pending_settlement"),
        "pending_payout_count": sum(1 for r in recons if r["outcome"] == "pending_payout"),
        "total_variance_inr": round(total_variance, 2),
        "mdr_drift_count": mdr_drift_count,
        "payout_count": len(payouts),
    }

    await _emit_event("PIPELINE_STEP", {
        "run_id": run_id,
        "step": "RECON",
        "status": "completed",
    })
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
