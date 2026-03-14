"""
Mock Pine Labs server — FastAPI on port 9100.
Mimics Pine Labs UAT API with deterministic responses.

Controllable failures via MOCK_CONFIG["fail_rules"]:
  [{"vendor_contains": "Surgical", "rail": "neft"}]
  Any payment whose merchant_payment_reference contains the vendor string AND
  uses the matching rail will return status FAILED.

Run with:
  uvicorn mock_pine:app --port 9100
"""
import itertools
import time
from datetime import datetime, timezone, timedelta
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI(title="Mock Pine Labs", version="1.0.0")

# ---------------------------------------------------------------------------
# Controllable failure config — mutate at runtime for demo scenarios
# ---------------------------------------------------------------------------
MOCK_CONFIG: dict[str, Any] = {
    "fail_rules": [
        # Example: {"vendor_contains": "Surgical", "rail": "neft"}
    ]
}

# ---------------------------------------------------------------------------
# In-memory state
# ---------------------------------------------------------------------------
_order_counter = itertools.count(1)
_orders: dict[str, dict] = {}       # order_id -> order record
_payments: dict[str, list] = {}     # order_id -> list of payment records
_refunds: dict[str, list] = {}      # order_id -> list of refund records

MOCK_TOKEN = "mock-access-token-priya-hackathon"
MOCK_TOKEN_EXPIRY = "2099-12-31T23:59:59Z"

# Pre-configured realistic settlement data
_SETTLEMENTS = [
    {
        "utr": "UTR2026031400001",
        "settlement_date": "2026-03-14",
        "gross_amount": 250000,
        "mdr_amount": 4500,
        "net_amount": 245500,
        "currency": "INR",
        "order_count": 3,
        "orders": ["v1-mock-001-aa-PRIYA", "v1-mock-002-aa-PRIYA"],
    },
    {
        "utr": "UTR2026031300001",
        "settlement_date": "2026-03-13",
        "gross_amount": 180000,
        "mdr_amount": 3240,
        "net_amount": 176760,
        "currency": "INR",
        "order_count": 2,
        "orders": ["v1-mock-003-aa-PRIYA"],
    },
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _new_order_id() -> str:
    n = next(_order_counter)
    return f"v1-mock-{n:03d}-aa-PRIYA"


def _should_fail(merchant_payment_reference: str, payment_method: str) -> bool:
    ref_lower = merchant_payment_reference.lower()
    method_lower = payment_method.lower()
    for rule in MOCK_CONFIG.get("fail_rules", []):
        vendor = rule.get("vendor_contains", "").lower()
        rail = rule.get("rail", "").lower()
        if vendor and vendor in ref_lower:
            if not rail or rail in method_lower:
                return True
    return False


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

@app.post("/auth/v1/token")
async def generate_token(request: Request):
    return JSONResponse({
        "access_token": MOCK_TOKEN,
        "token_type": "Bearer",
        "expires_in": 3600,
        "expires_at": MOCK_TOKEN_EXPIRY,
    })


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------

@app.post("/pay/v1/orders")
async def create_order(request: Request):
    body = await request.json()
    order_id = _new_order_id()
    merchant_order_reference = body.get("merchant_order_reference", "unknown")
    amount = body.get("order_amount", {})
    record = {
        "order_id": order_id,
        "merchant_order_reference": merchant_order_reference,
        "order_amount": amount,
        "pre_auth": body.get("pre_auth", False),
        "status": "CREATED",
        "callback_url": body.get("callback_url"),
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
    }
    _orders[order_id] = record
    _payments[order_id] = []
    return JSONResponse({"data": record}, status_code=201)


@app.get("/pay/v1/orders/{order_id}")
async def get_order(order_id: str):
    if order_id not in _orders:
        return JSONResponse({"error": "order not found", "order_id": order_id}, status_code=404)
    order = dict(_orders[order_id])
    order["payments"] = _payments.get(order_id, [])
    return JSONResponse({"data": order})


@app.put("/pay/v1/orders/{order_id}/capture")
async def capture_order(order_id: str, request: Request):
    if order_id not in _orders:
        return JSONResponse({"error": "order not found"}, status_code=404)
    body = await request.json()
    _orders[order_id]["status"] = "CAPTURED"
    _orders[order_id]["captured_at"] = _now_iso()
    _orders[order_id]["merchant_capture_reference"] = body.get("merchant_capture_reference")
    _orders[order_id]["capture_amount"] = body.get("capture_amount")
    _orders[order_id]["updated_at"] = _now_iso()
    return JSONResponse({"data": _orders[order_id]})


@app.put("/pay/v1/orders/{order_id}/cancel")
async def cancel_order(order_id: str):
    if order_id not in _orders:
        return JSONResponse({"error": "order not found"}, status_code=404)
    _orders[order_id]["status"] = "CANCELLED"
    _orders[order_id]["cancelled_at"] = _now_iso()
    _orders[order_id]["updated_at"] = _now_iso()
    return JSONResponse({"data": _orders[order_id]})


# ---------------------------------------------------------------------------
# Payments
# ---------------------------------------------------------------------------

@app.post("/pay/v1/orders/{order_id}/payments")
async def create_payment(order_id: str, request: Request):
    if order_id not in _orders:
        return JSONResponse({"error": "order not found"}, status_code=404)

    body = await request.json()
    payment_method = body.get("payment_method", "UPI")
    merchant_payment_reference = body.get("merchant_payment_reference", "")

    failed = _should_fail(merchant_payment_reference, payment_method)
    status = "FAILED" if failed else "PROCESSED"

    payment_id = f"pay-mock-{len(_payments[order_id]) + 1:03d}-{order_id}"
    payment = {
        "payment_id": payment_id,
        "order_id": order_id,
        "merchant_payment_reference": merchant_payment_reference,
        "payment_amount": body.get("payment_amount"),
        "payment_method": payment_method,
        "status": status,
        "failure_reason": "MOCK_RULE_TRIGGERED" if failed else None,
        "upi_details": body.get("upi_details"),
        "netbanking_details": body.get("netbanking_details"),
        "created_at": _now_iso(),
    }
    _payments[order_id].append(payment)

    if status == "PROCESSED":
        _orders[order_id]["status"] = "PROCESSED"
        _orders[order_id]["updated_at"] = _now_iso()

    return JSONResponse({"data": payment}, status_code=200 if not failed else 200)


# ---------------------------------------------------------------------------
# Refunds
# ---------------------------------------------------------------------------

@app.post("/pay/v1/refunds/{order_id}")
async def create_refund(order_id: str, request: Request):
    if order_id not in _orders:
        return JSONResponse({"error": "order not found"}, status_code=404)
    body = await request.json()
    if order_id not in _refunds:
        _refunds[order_id] = []
    refund_id = f"ref-mock-{len(_refunds[order_id]) + 1:03d}-{order_id}"
    refund = {
        "refund_id": refund_id,
        "order_id": order_id,
        "merchant_order_reference": body.get("merchant_order_reference"),
        "refund_amount": body.get("refund_amount"),
        "status": "REFUND_INITIATED",
        "created_at": _now_iso(),
    }
    _refunds[order_id].append(refund)
    _orders[order_id]["status"] = "REFUND_INITIATED"
    _orders[order_id]["updated_at"] = _now_iso()
    return JSONResponse({"data": refund}, status_code=201)


# ---------------------------------------------------------------------------
# Settlements
# ---------------------------------------------------------------------------

@app.get("/settlements/v1/list")
async def get_all_settlements(
    start_date: str = "",
    end_date: str = "",
    page: int = 1,
    per_page: int = 10,
):
    # Dynamically generate settlements from actual processed orders
    settlements = []
    utr_counter = 1
    for order_id, order in _orders.items():
        if order.get("status") in ("PROCESSED", "CAPTURED"):
            amount_paisa = order.get("order_amount", {}).get("value", 0)
            mdr_paisa = int(amount_paisa * 0.018)  # 1.8% MDR
            settlements.append({
                "utr": f"UTR{_now_iso()[:10].replace('-','')}{utr_counter:05d}",
                "settlement_date": _now_iso()[:10],
                "gross_amount": amount_paisa,
                "mdr_amount": mdr_paisa,
                "net_amount": amount_paisa - mdr_paisa,
                "currency": "INR",
                "order_count": 1,
                "orders": [order_id],
                "bank_account": "XXXX1234",
            })
            utr_counter += 1

    # If no dynamic settlements, fall back to pre-configured
    if not settlements:
        settlements = _SETTLEMENTS

    start_idx = (page - 1) * per_page
    end_idx = start_idx + per_page
    return JSONResponse({
        "data": settlements[start_idx:end_idx],
        "pagination": {
            "page": page,
            "per_page": per_page,
            "total": len(settlements),
        },
    })


@app.get("/settlements/v1/detail/{utr}")
async def get_settlement_by_utr(utr: str, page: int = 1, per_page: int = 10):
    for s in _SETTLEMENTS:
        if s["utr"] == utr:
            return JSONResponse({"data": s})
    return JSONResponse({"error": "UTR not found", "utr": utr}, status_code=404)


# ---------------------------------------------------------------------------
# Payment Link
# ---------------------------------------------------------------------------

@app.post("/pay/v1/paymentlink")
async def create_payment_link(request: Request):
    body = await request.json()
    ref = body.get("merchant_payment_link_reference", "unknown")
    link_id = f"lnk-mock-{ref}"
    return JSONResponse({
        "data": {
            "payment_link_id": link_id,
            "merchant_payment_link_reference": ref,
            "amount": body.get("amount"),
            "description": body.get("description", ""),
            "payment_link_url": f"http://localhost:9100/pay/link/{link_id}",
            "status": "ACTIVE",
            "created_at": _now_iso(),
        }
    }, status_code=201)


# ---------------------------------------------------------------------------
# Hosted Checkout
# ---------------------------------------------------------------------------

@app.post("/checkout/v1/orders")
async def hosted_checkout(request: Request):
    body = await request.json()
    order_id = _new_order_id()
    merchant_order_reference = body.get("merchant_order_reference", "unknown")
    record = {
        "order_id": order_id,
        "merchant_order_reference": merchant_order_reference,
        "order_amount": body.get("order_amount"),
        "status": "CREATED",
        "checkout_url": f"http://localhost:9100/checkout/{order_id}",
        "callback_url": body.get("callback_url"),
        "created_at": _now_iso(),
    }
    _orders[order_id] = record
    _payments[order_id] = []
    return JSONResponse({"data": record}, status_code=201)


# ---------------------------------------------------------------------------
# Debug / control endpoints (not in real Pine Labs API)
# ---------------------------------------------------------------------------

@app.get("/_mock/config")
async def get_mock_config():
    return JSONResponse(MOCK_CONFIG)


@app.post("/_mock/config")
async def set_mock_config(request: Request):
    """Update MOCK_CONFIG at runtime for demo scenarios."""
    body = await request.json()
    MOCK_CONFIG.update(body)
    return JSONResponse({"status": "updated", "config": MOCK_CONFIG})


@app.delete("/_mock/reset")
async def reset_mock():
    """Reset all in-memory state (orders, payments, refunds) for a fresh demo."""
    global _order_counter
    _order_counter = itertools.count(1)
    _orders.clear()
    _payments.clear()
    _refunds.clear()
    MOCK_CONFIG["fail_rules"] = []
    return JSONResponse({"status": "reset"})
