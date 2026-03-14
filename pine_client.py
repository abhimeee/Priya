"""
Pine Labs async HTTP client.
All monetary amounts are in PAISA (100 paisa = Rs 1).
If PINE_MOCK=true, all requests are redirected to http://localhost:9100.
"""
import os
import uuid
from datetime import datetime, timezone

import httpx

PINE_BASE_URL = os.getenv("PINE_BASE_URL", "https://pluraluat.v2.pinepg.in/api")
PINE_CLIENT_ID = os.getenv("PINE_CLIENT_ID", "")
PINE_CLIENT_SECRET = os.getenv("PINE_CLIENT_SECRET", "")
_PINE_MID_RAW = os.getenv("PINE_MID", "")
PINE_MID = int(_PINE_MID_RAW) if _PINE_MID_RAW.isdigit() else _PINE_MID_RAW
PINE_MOCK = os.getenv("PINE_MOCK", "false").lower() == "true"

MOCK_BASE_URL = "http://localhost:9100"


def _base_url() -> str:
    return MOCK_BASE_URL if PINE_MOCK else PINE_BASE_URL


def _common_headers(token: str | None = None) -> dict:
    headers = {
        "Content-Type": "application/json",
        "Request-ID": str(uuid.uuid4()),
        "Request-Timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


class PineLabs:
    def __init__(self, timeout: float = 30.0):
        self._timeout = timeout

    async def generate_token(self) -> dict:
        url = f"{_base_url()}/auth/v1/token"
        body = {
            "client_id": PINE_CLIENT_ID,
            "client_secret": PINE_CLIENT_SECRET,
            "grant_type": "client_credentials",
        }
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(url, json=body, headers=_common_headers())
            resp.raise_for_status()
            return resp.json()

    async def create_order(
        self,
        token: str,
        merchant_order_reference: str,
        amount_paisa: int,
        pre_auth: bool = False,
        callback_url: str | None = None,
    ) -> dict:
        url = f"{_base_url()}/pay/v1/orders"
        body: dict = {
            "merchant_id": PINE_MID,
            "merchant_order_reference": merchant_order_reference,
            "order_amount": {"value": amount_paisa, "currency": "INR"},
            "pre_auth": pre_auth,
            "purchase_details": {
                "customer": {
                    "email_id": "payments@priya.ai",
                    "first_name": "PRIYA",
                    "last_name": "Agent",
                    "customer_id": "PRIYA-001",
                    "mobile_number": "9999999999",
                },
            },
        }
        if callback_url:
            body["callback_url"] = callback_url
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(url, json=body, headers=_common_headers(token))
            if resp.status_code >= 400:
                detail = resp.text[:500]
                raise httpx.HTTPStatusError(
                    f"Pine Labs create_order {resp.status_code}: {detail}",
                    request=resp.request, response=resp,
                )
            return resp.json()

    async def get_order(self, token: str, order_id: str) -> dict:
        url = f"{_base_url()}/pay/v1/orders/{order_id}"
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.get(url, headers=_common_headers(token))
            resp.raise_for_status()
            return resp.json()

    async def capture_order(
        self,
        token: str,
        order_id: str,
        merchant_capture_reference: str,
        capture_amount_paisa: int,
    ) -> dict:
        url = f"{_base_url()}/pay/v1/orders/{order_id}/capture"
        body = {
            "merchant_capture_reference": merchant_capture_reference,
            "capture_amount": {"value": capture_amount_paisa, "currency": "INR"},
        }
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.put(url, json=body, headers=_common_headers(token))
            resp.raise_for_status()
            return resp.json()

    async def cancel_order(self, token: str, order_id: str) -> dict:
        url = f"{_base_url()}/pay/v1/orders/{order_id}/cancel"
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.put(url, json={}, headers=_common_headers(token))
            resp.raise_for_status()
            return resp.json()

    async def create_payment_upi_intent(
        self,
        token: str,
        order_id: str,
        merchant_payment_reference: str,
        amount_paisa: int,
    ) -> dict:
        url = f"{_base_url()}/pay/v1/orders/{order_id}/payments"
        body = {
            "merchant_payment_reference": merchant_payment_reference,
            "payment_amount": {"value": amount_paisa, "currency": "INR"},
            "payment_method": "UPI",
            "upi_details": {"txn_mode": "INTENT"},
        }
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(url, json=body, headers=_common_headers(token))
            resp.raise_for_status()
            return resp.json()

    async def create_payment_upi_collect(
        self,
        token: str,
        order_id: str,
        merchant_payment_reference: str,
        amount_paisa: int,
        payer_vpa: str,
    ) -> dict:
        url = f"{_base_url()}/pay/v1/orders/{order_id}/payments"
        body = {
            "merchant_payment_reference": merchant_payment_reference,
            "payment_amount": {"value": amount_paisa, "currency": "INR"},
            "payment_method": "UPI",
            "upi_details": {
                "txn_mode": "COLLECT",
                "payer": {"vpa": payer_vpa},
            },
        }
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(url, json=body, headers=_common_headers(token))
            resp.raise_for_status()
            return resp.json()

    async def create_payment_netbanking(
        self,
        token: str,
        order_id: str,
        merchant_payment_reference: str,
        amount_paisa: int,
        pay_code: str = "NB1493",
    ) -> dict:
        url = f"{_base_url()}/pay/v1/orders/{order_id}/payments"
        body = {
            "merchant_payment_reference": merchant_payment_reference,
            "payment_amount": {"value": amount_paisa, "currency": "INR"},
            "payment_method": "NETBANKING",
            "netbanking_details": {"pay_code": pay_code},
        }
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(url, json=body, headers=_common_headers(token))
            resp.raise_for_status()
            return resp.json()

    async def create_refund(
        self,
        token: str,
        order_id: str,
        merchant_order_reference: str,
        amount_paisa: int,
    ) -> dict:
        url = f"{_base_url()}/pay/v1/refunds/{order_id}"
        body = {
            "merchant_order_reference": merchant_order_reference,
            "refund_amount": {"value": amount_paisa, "currency": "INR"},
        }
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(url, json=body, headers=_common_headers(token))
            resp.raise_for_status()
            return resp.json()

    async def get_all_settlements(
        self,
        token: str,
        start_date: str,
        end_date: str,
        page: int = 1,
        per_page: int = 10,
    ) -> dict:
        """Fetch settlements list.

        Pine Labs UAT/PROD requires dates in the format ``YYYY-MM-DDTHH:MM:SS``
        (no timezone suffix — Z or +05:30 will return INVALID_DATE 400).
        Callers may pass plain ``YYYY-MM-DD`` strings; this method will
        normalise them automatically.

        Confirmed via MCP docs (pinelabs-mcp-server v7.0.0) and live testing:
        - Correct:  ``2026-03-01T00:00:00``
        - Wrong:    ``2026-03-01``, ``2026-03-01T00:00:00Z``, epoch ms
        """
        # Normalise plain YYYY-MM-DD → YYYY-MM-DDTHH:MM:SS
        if len(start_date) == 10:
            start_date = f"{start_date}T00:00:00"
        if len(end_date) == 10:
            end_date = f"{end_date}T23:59:59"

        url = f"{_base_url()}/settlements/v1/list"
        params = {
            "start_date": start_date,
            "end_date": end_date,
            "page": page,
            "per_page": per_page,
        }
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.get(url, params=params, headers=_common_headers(token))
            resp.raise_for_status()
            return resp.json()

    async def get_settlement_by_utr(
        self,
        token: str,
        utr: str,
        page: int = 1,
        per_page: int = 10,
    ) -> dict:
        url = f"{_base_url()}/settlements/v1/detail/{utr}"
        params = {"page": page, "per_page": per_page}
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.get(url, params=params, headers=_common_headers(token))
            resp.raise_for_status()
            return resp.json()

    async def create_payment_link(
        self,
        token: str,
        amount_paisa: int,
        merchant_payment_link_reference: str,
        description: str = "",
    ) -> dict:
        url = f"{_base_url()}/pay/v1/paymentlink"
        body = {
            "merchant_id": PINE_MID,
            "amount": {"value": amount_paisa, "currency": "INR"},
            "merchant_payment_link_reference": merchant_payment_link_reference,
            "description": description or "PRIYA vendor payment",
            "customer": {
                "email_id": "payments@priya.ai",
                "first_name": "PRIYA",
                "last_name": "Agent",
                "customer_id": "PRIYA-001",
                "mobile_number": "9999999999",
            },
        }
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(url, json=body, headers=_common_headers(token))
            if resp.status_code >= 400:
                detail = resp.text[:500]
                raise httpx.HTTPStatusError(
                    f"Pine Labs create_payment_link {resp.status_code}: {detail}",
                    request=resp.request, response=resp,
                )
            return resp.json()

    async def hosted_checkout(
        self,
        token: str,
        merchant_order_reference: str,
        amount_paisa: int,
        callback_url: str,
    ) -> dict:
        url = f"{_base_url()}/checkout/v1/orders"
        body = {
            "merchant_id": PINE_MID,
            "merchant_order_reference": merchant_order_reference,
            "order_amount": {"value": amount_paisa, "currency": "INR"},
            "callback_url": callback_url,
            "purchase_details": {
                "customer": {
                    "email_id": "payments@priya.ai",
                    "first_name": "PRIYA",
                    "last_name": "Agent",
                    "customer_id": "PRIYA-001",
                    "mobile_number": "9999999999",
                },
            },
        }
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(url, json=body, headers=_common_headers(token))
            resp.raise_for_status()
            return resp.json()
