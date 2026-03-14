# PRIYA Backend Technical Specification

> Proactive Revenue & Invoice Yield Automator
> Python Backend — Claude Agent SDK + Pine Labs API + SQLite
> Pine Labs AI Hackathon | v1.0

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [Tech Stack & Dependencies](#2-tech-stack--dependencies)
3. [File Structure](#3-file-structure)
4. [Pine Labs API Client (`pine_client.py`)](#4-pine-labs-api-client)
5. [Database Layer (`db.py` + `priya_init.sql`)](#5-database-layer)
6. [Priority Scorer (`scorer.py`)](#6-priority-scorer)
7. [PRIYA MCP Server (`priya_mcp_server.py`)](#7-priya-mcp-server)
8. [Claude Agent SDK Orchestrator (`agent.py`)](#8-claude-agent-sdk-orchestrator)
9. [FastAPI Server (`main.py`)](#9-fastapi-server)
10. [Mock Pine Labs Server (`mock_pine.py`)](#10-mock-pine-labs-server)
11. [Persona Configuration](#11-persona-configuration)
12. [Invoice Data](#12-invoice-data)
13. [WebSocket Event Contract](#13-websocket-event-contract)
14. [Environment & Configuration](#14-environment--configuration)
15. [Error Handling & Recovery](#15-error-handling--recovery)
16. [Sprint Build Order](#16-sprint-build-order)

---

## 1. Architecture Overview

```
                    React Frontend
                         |
                    WebSocket + REST
                         |
               main.py (FastAPI Server)
              /          |           \
         REST API    WebSocket    Agent Sessions
              \          |           /
               agent.py (Claude Agent SDK)
                    |           |
            Built-in Tools    MCP (stdio)
            (Read, Bash,      |
             Glob)        priya_mcp_server.py
                              |
                 +------------+------------+
                 |            |            |
           pine_client.py   db.py    WebSocket Emitter
                 |            |            |
         Pine Labs REST   SQLite DB    Frontend Events
         (or mock_pine)   (priya.db)
```

### Data Flow — Single Run

```
1. User types NLP instruction in chat → POST /run
2. FastAPI spawns agent session → agent.py query()
3. Claude Agent SDK loads system prompt (persona + DB schema + rules)
4. Agent calls MCP tools autonomously in a loop:
   a. mcp__priya__load_invoices() → parse CSV, upsert vendors
   b. mcp__priya__generate_token() → Pine Labs auth
   c. mcp__priya__score_vendors() → priority ranking
   d. mcp__priya__request_policy_approval() → BLOCKS until frontend approves
   e. For each vendor (priority order):
      - mcp__priya__create_order() → Pine Labs API + DB write + WS event
      - mcp__priya__create_payment() → Pine Labs API + DB write + WS event
      - On failure: mcp__priya__switch_rail_and_retry()
      - On Schedule H: mcp__priya__request_escalation_decision() → BLOCKS
   f. mcp__priya__run_settlements() → fetch + match
   g. mcp__priya__run_reconciliation() → cross-check + write audit
   h. mcp__priya__finalize_run() → summary metrics
5. Agent emits CANVAS_STATE: audit → run complete
```

---

## 2. Tech Stack & Dependencies

### `requirements.txt`

```
claude-agent-sdk>=1.0.0
anthropic>=0.50.0
fastapi>=0.115.0
uvicorn>=0.34.0
websockets>=14.0
httpx>=0.28.0
aiosqlite>=0.21.0
pyyaml>=6.0
python-dotenv>=1.0.0
mcp>=1.0.0
pydantic>=2.10.0
```

### Runtime Requirements

- Python 3.11+
- Node.js 22+ (for `npx mcp-remote` if needed)
- SQLite 3 (bundled with Python)
- `ANTHROPIC_API_KEY` environment variable

---

## 3. File Structure

```
claude-agent-sdk/
├── main.py                     # FastAPI application entry point
├── agent.py                    # Claude Agent SDK orchestrator
├── priya_mcp_server.py         # Custom MCP server (all domain tools)
├── pine_client.py              # Pine Labs REST API client (httpx)
├── mock_pine.py                # Mock Pine Labs server for demo
├── db.py                       # SQLite init + async CRUD operations
├── scorer.py                   # Priority scoring + credit float logic
├── priya_init.sql              # Database schema DDL
├── requirements.txt            # Python dependencies
├── CLAUDE.md                   # Agent system prompt / project context
├── .env                        # Secrets (gitignored)
├── .gitignore
├── personas/
│   ├── hospital.yaml           # Hospital persona rules
│   └── kirana.yaml             # Kirana persona rules
└── invoices/
    ├── hospital_invoices.csv   # Demo hospital invoice data
    └── kirana_invoices.csv     # Demo kirana invoice data
```

---

## 4. Pine Labs API Client

### File: `pine_client.py`

HTTP client using `httpx.AsyncClient` for all Pine Labs REST API calls. Supports real API and mock mode via environment variable.

### Configuration

```python
BASE_URL = os.getenv("PINE_BASE_URL", "https://pluraluat.v2.pinepg.in/api")  # UAT default
PINE_MID = os.getenv("PINE_MID")               # 121495
CLIENT_ID = os.getenv("PINE_CLIENT_ID")         # b74c51a7-da84-4f2d-b3b2-566ea676fc64
CLIENT_SECRET = os.getenv("PINE_CLIENT_SECRET")  # stored in .env
USE_MOCK = os.getenv("PINE_MOCK", "false").lower() == "true"
```

### Common Headers (injected on every request)

```python
def _headers(self, token: str | None = None) -> dict:
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}" if token else "",
        "Request-ID": str(uuid.uuid4()),
        "Request-Timestamp": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
    }
```

### API Methods

#### 4.1 `generate_token() -> dict`

```
POST /auth/v1/token

Request Body:
{
    "client_id": "b74c51a7-da84-4f2d-b3b2-566ea676fc64",
    "client_secret": "<secret>",
    "grant_type": "client_credentials"
}

Response 200:
{
    "access_token": "eyJhbGci...",
    "expires_at": "2024-08-28T15:00:12.213Z"
}
```

**Notes:** Token cached for session. Refresh when `expires_at` is near.

#### 4.2 `create_order(token, merchant_order_reference, amount_paisa, pre_auth, callback_url) -> dict`

```
POST /pay/v1/orders

Request Body:
{
    "merchant_order_reference": "run_20250114_001_vnd_insulin",  // UUID format
    "order_amount": {
        "value": 4500000,       // Amount in PAISA (Rs 45,000 = 4500000)
        "currency": "INR"
    },
    "pre_auth": false,          // true for Schedule H
    "allowed_payment_methods": ["CARD", "UPI", "NETBANKING", "WALLET"],
    "notes": "PRIYA automated payment",
    "callback_url": "https://<our-server>/webhook/pinelabs"
}

Response 200:
{
    "data": {
        "order_id": "v1-4405071524-aa-qlAtAf",       // Pine Labs spine
        "merchant_order_reference": "run_20250114_001_vnd_insulin",
        "type": "CHARGE",
        "status": "CREATED",
        "merchant_id": "121495",
        "order_amount": { "value": 4500000, "currency": "INR" },
        "pre_auth": false
    }
}
```

**CRITICAL:** All amounts in **paisa**. Rs 1 = 100 paisa. Rs 45,000 = 4,500,000 paisa.

#### 4.3 `create_payment_upi_intent(token, order_id, merchant_payment_reference, amount_paisa) -> dict`

```
POST /pay/v1/orders/{order_id}/payments

Request Body:
{
    "payments": [{
        "merchant_payment_reference": "pay_uuid_attempt_1",
        "payment_method": "UPI",
        "payment_amount": {
            "value": 4500000,
            "currency": "INR"
        },
        "payment_option": {
            "upi_details": {
                "txn_mode": "INTENT"
            }
        }
    }]
}

Response 200:
{
    "data": {
        "order_id": "v1-4405071524-aa-qlAtAf",
        "status": "PENDING",
        "payments": [{
            "id": "pay-v1-...",
            "merchant_payment_reference": "pay_uuid_attempt_1",
            "status": "PENDING",
            "payment_method": "UPI",
            "payment_amount": { "value": 4500000, "currency": "INR" }
        }]
    }
}
```

#### 4.4 `create_payment_upi_collect(token, order_id, merchant_payment_reference, amount_paisa, payer_vpa) -> dict`

```
POST /pay/v1/orders/{order_id}/payments

Request Body:
{
    "payments": [{
        "merchant_payment_reference": "pay_uuid_attempt_2",
        "payment_method": "UPI",
        "payment_amount": { "value": 4500000, "currency": "INR" },
        "payment_option": {
            "upi_details": {
                "txn_mode": "COLLECT",
                "payer": {
                    "vpa": "insulindepot@okhdfc"
                }
            }
        }
    }]
}
```

#### 4.5 `create_payment_netbanking(token, order_id, merchant_payment_reference, amount_paisa, pay_code) -> dict`

```
POST /pay/v1/orders/{order_id}/payments

Request Body:
{
    "payments": [{
        "merchant_payment_reference": "pay_uuid_attempt_1",
        "payment_method": "NETBANKING",
        "payment_amount": { "value": 4500000, "currency": "INR" },
        "payment_option": {
            "netbanking_details": {
                "pay_code": "NB1493"
            }
        }
    }]
}
```

#### 4.6 `get_order(token, order_id) -> dict`

```
GET /pay/v1/orders/{order_id}

Response 200:
{
    "data": {
        "order_id": "v1-...",
        "status": "PROCESSED",    // CREATED | PENDING | AUTHORIZED | PROCESSED | CANCELLED | FAILED
        "payments": [{
            "id": "pay-v1-...",
            "status": "PROCESSED",
            "payment_method": "UPI"
        }]
    }
}
```

#### 4.7 `capture_order(token, order_id, merchant_capture_reference, capture_amount_paisa) -> dict`

```
PUT /pay/v1/orders/{order_id}/capture

Request Body:
{
    "merchant_capture_reference": "cap_uuid",
    "capture_amount": {
        "value": 4500000,
        "currency": "INR"
    }
}

Response: status transitions to "PROCESSED"
```

#### 4.8 `cancel_order(token, order_id) -> dict`

```
PUT /pay/v1/orders/{order_id}/cancel

Request Body: {} (empty)

Response: status transitions to "CANCELLED"
```

#### 4.9 `create_refund(token, order_id, merchant_order_reference, amount_paisa) -> dict`

```
POST /pay/v1/refunds/{order_id}

Request Body:
{
    "merchant_order_reference": "refund_uuid",
    "order_amount": {
        "value": 4500000,
        "currency": "INR"
    }
}

Response 200:
{
    "data": {
        "order_id": "v1-...(new refund order)...",
        "parent_order_id": "v1-...(original)...",
        "type": "REFUND",
        "status": "CREATED"
    }
}
```

#### 4.10 `get_all_settlements(token, start_date, end_date, page, per_page) -> dict`

```
GET /settlements/v1/list?start_date=2024-10-01T00:00:00&end_date=2024-10-09T23:59:59&page=1&per_page=10

Response 200:
{
    "data": [{
        "total_amount": 0.96,
        "actual_transaction_amount": 2,
        "total_deduction_amount": 0.08,
        "total_transactions_count": 2,
        "last_processed_date": "2024-10-09T07:00:00",
        "settled_date": "2024-10-09T10:59:46",
        "utr_number": "410092786849",
        "programs": ["UPI"],
        "system": "PG"
    }]
}
```

#### 4.11 `get_settlement_by_utr(token, utr_number, page, per_page) -> dict`

```
GET /settlements/v1/detail/{utr}?page=1&per_page=10

Response 200:
{
    "utr_number": "410092786849",
    "bank_name": "HDFC Bank Ltd",
    "bank_acc_number": "04992990009595",
    "programs": ["UPI"],
    "total_transaction_count": 2,
    "total_transaction_amount": 2,
    "actual_transaction_amount": 2,
    "total_refund": 0,
    "total_chargeback_recovery_amount": 0,
    "total_loan_recovery_amount": 0,
    "total_mdr_amount": 0.08,
    "total_tax_on_mdr": 0.014,
    "net_settlement_amount": 0.96,
    "settled_date": "2024-10-09T10:59:46",
    "last_processed_date": "2024-10-09T07:00:00"
}
```

#### 4.12 `create_payment_link(token, amount_paisa, merchant_payment_link_reference, ...) -> dict`

```
POST /pay/v1/paymentlink

Request Body:
{
    "amount": {
        "value": 4500000,
        "currency": "INR"
    },
    "merchant_payment_link_reference": "plink_uuid",
    "description": "Payment for Insulin Depot - PRIYA Run run_20250114_001"
}

Response 200:
{
    "payment_link": "https://shortener.v2.pinepg.in/PLUTUS/3rh4jtd",
    "payment_link_id": "pl-v1-250306082755-aa-uT0noy",
    "status": "CREATED",
    "amount": { "value": 4500000, "currency": "INR" }
}
```

#### 4.13 `hosted_checkout_create(token, merchant_order_reference, amount_paisa, callback_url) -> dict`

```
POST /checkout/v1/orders

Request Body:
{
    "merchant_order_reference": "checkout_uuid",
    "order_amount": { "value": 4500000, "currency": "INR" },
    "integration_mode": "IFRAME",
    "pre_auth": false,
    "allowed_payment_methods": ["CARD", "UPI", "NETBANKING", "WALLET"],
    "callback_url": "https://<server>/webhook/checkout"
}

Response 200:
{
    "token": "<<Redirect Token>>",
    "order_id": "v1-...",
    "redirect_url": "https://api.pluralonline.com/api/v3/checkout-bff/redirect/checkout?token=..."
}
```

### Rail Mapping

| PRIYA Rail Name | Pine Labs API Method | Payment Method |
|-----------------|---------------------|----------------|
| `neft` | `create_payment_netbanking()` | NETBANKING |
| `upi` | `create_payment_upi_collect()` | UPI (COLLECT) |
| `upi_intent` | `create_payment_upi_intent()` | UPI (INTENT) |
| `hosted_checkout` | `hosted_checkout_create()` | All methods |
| `payment_link` | `create_payment_link()` | All methods (last resort) |

---

## 5. Database Layer

### File: `priya_init.sql`

Copied verbatim from PRIYA Master Spec v3. Run once: `sqlite3 priya.db < priya_init.sql`

```sql
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS vendors (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  persona TEXT NOT NULL,
  category TEXT NOT NULL,
  upi_id TEXT,
  bank_account TEXT,
  ifsc TEXT,
  preferred_rail TEXT NOT NULL DEFAULT 'upi',
  credit_days INTEGER DEFAULT 0,
  vendor_type TEXT NOT NULL DEFAULT 'established',
  drug_schedule TEXT,
  is_compliant INTEGER DEFAULT 1,
  created_at DATETIME NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY,
  persona TEXT NOT NULL,
  instruction TEXT NOT NULL,
  invoice_source TEXT NOT NULL,
  pine_token TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  total_vendors INTEGER NOT NULL,
  total_amount REAL NOT NULL,
  paid_amount REAL DEFAULT 0,
  deferred_amount REAL DEFAULT 0,
  float_saved REAL DEFAULT 0,
  started_at DATETIME NOT NULL,
  completed_at DATETIME,
  approved_at DATETIME
);

CREATE TABLE IF NOT EXISTS orders (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(id),
  vendor_id TEXT NOT NULL REFERENCES vendors(id),
  pine_order_id TEXT NOT NULL,
  merchant_order_reference TEXT NOT NULL,
  amount REAL NOT NULL,
  priority_score INTEGER NOT NULL,
  priority_reason TEXT NOT NULL,
  action TEXT NOT NULL,
  pre_auth INTEGER DEFAULT 0,
  pine_status TEXT NOT NULL DEFAULT 'CREATED',
  escalation_flag TEXT,
  defer_reason TEXT,
  created_at DATETIME NOT NULL,
  updated_at DATETIME NOT NULL
);

CREATE TABLE IF NOT EXISTS payments (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(id),
  order_id TEXT NOT NULL REFERENCES orders(id),
  vendor_id TEXT NOT NULL REFERENCES vendors(id),
  pine_order_id TEXT NOT NULL,
  pine_payment_id TEXT,
  merchant_payment_reference TEXT NOT NULL,
  amount REAL NOT NULL,
  rail TEXT NOT NULL,
  attempt_number INTEGER DEFAULT 1,
  pine_status TEXT NOT NULL DEFAULT 'PENDING',
  failure_reason TEXT,
  recovery_action TEXT,
  webhook_event TEXT,
  request_id TEXT NOT NULL,
  initiated_at DATETIME NOT NULL,
  confirmed_at DATETIME
);

CREATE TABLE IF NOT EXISTS settlements (
  id TEXT PRIMARY KEY,
  run_id TEXT,
  pine_order_id TEXT NOT NULL,
  pine_settlement_id TEXT,
  utr_number TEXT NOT NULL,
  bank_account TEXT,
  last_processed_date DATETIME NOT NULL,
  expected_amount REAL NOT NULL,
  settled_amount REAL NOT NULL,
  platform_fee REAL DEFAULT 0,
  total_deduction_amount REAL DEFAULT 0,
  refund_debit REAL DEFAULT 0,
  fee_flagged INTEGER DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'pending',
  settled_at DATETIME,
  created_at DATETIME NOT NULL
);

CREATE TABLE IF NOT EXISTS reconciliations (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(id),
  order_id TEXT NOT NULL,
  payment_id TEXT NOT NULL,
  settlement_id TEXT,
  vendor_id TEXT NOT NULL,
  pine_order_id TEXT NOT NULL,
  merchant_order_reference TEXT NOT NULL,
  utr_number TEXT,
  persona TEXT NOT NULL,
  invoice_amount REAL NOT NULL,
  paid_amount REAL DEFAULT 0,
  settled_amount REAL DEFAULT 0,
  variance REAL DEFAULT 0,
  mdr_rate_actual REAL,
  mdr_rate_contracted REAL,
  mdr_drift_flagged INTEGER DEFAULT 0,
  rail_used TEXT,
  retries INTEGER DEFAULT 0,
  outcome TEXT NOT NULL,
  pre_auth_used INTEGER DEFAULT 0,
  agent_reasoning TEXT NOT NULL,
  ca_notes TEXT,
  created_at DATETIME NOT NULL
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_orders_pine    ON orders(pine_order_id);
CREATE INDEX IF NOT EXISTS idx_orders_mor     ON orders(merchant_order_reference);
CREATE INDEX IF NOT EXISTS idx_orders_run     ON orders(run_id);
CREATE INDEX IF NOT EXISTS idx_orders_status  ON orders(pine_status);
CREATE INDEX IF NOT EXISTS idx_pay_pine       ON payments(pine_order_id);
CREATE INDEX IF NOT EXISTS idx_pay_run        ON payments(run_id);
CREATE INDEX IF NOT EXISTS idx_pay_status     ON payments(pine_status);
CREATE INDEX IF NOT EXISTS idx_set_utr        ON settlements(utr_number);
CREATE INDEX IF NOT EXISTS idx_set_pine       ON settlements(pine_order_id);
CREATE INDEX IF NOT EXISTS idx_set_date       ON settlements(last_processed_date);
CREATE INDEX IF NOT EXISTS idx_rec_run        ON reconciliations(run_id);
CREATE INDEX IF NOT EXISTS idx_rec_persona    ON reconciliations(persona);
CREATE INDEX IF NOT EXISTS idx_rec_date       ON reconciliations(created_at);
CREATE INDEX IF NOT EXISTS idx_rec_outcome    ON reconciliations(outcome);
```

### File: `db.py`

Async SQLite wrapper using `aiosqlite`.

```python
class PriyaDB:
    def __init__(self, db_path: str = "./priya.db"):
        self.db_path = db_path

    async def init(self):
        """Run priya_init.sql to create tables."""

    # --- Vendors ---
    async def upsert_vendor(self, vendor: dict) -> str
    async def get_vendor(self, vendor_id: str) -> dict
    async def get_vendors_by_persona(self, persona: str) -> list[dict]

    # --- Runs ---
    async def create_run(self, run_id, persona, instruction, invoice_source, pine_token, total_vendors, total_amount) -> str
    async def update_run_status(self, run_id, status, **kwargs) -> None
    async def get_run(self, run_id) -> dict

    # --- Orders ---
    async def insert_order(self, id, run_id, vendor_id, pine_order_id, merchant_order_reference, amount, priority_score, priority_reason, action, pre_auth=False) -> str
    async def update_order_status(self, order_id, pine_status, **kwargs) -> None
    async def get_orders_by_run(self, run_id) -> list[dict]
    async def get_order_by_pine_id(self, pine_order_id) -> dict

    # --- Payments ---
    async def insert_payment(self, id, run_id, order_id, vendor_id, pine_order_id, merchant_payment_reference, amount, rail, request_id, attempt_number=1) -> str
    async def update_payment_status(self, payment_id, pine_status, **kwargs) -> None
    async def get_payments_by_order(self, order_id) -> list[dict]

    # --- Settlements ---
    async def insert_settlement(self, id, pine_order_id, utr_number, last_processed_date, expected_amount, settled_amount, **kwargs) -> str
    async def get_settlements_by_run(self, run_id) -> list[dict]

    # --- Reconciliations ---
    async def insert_reconciliation(self, data: dict) -> str
    async def get_reconciliations_by_run(self, run_id) -> list[dict]

    # --- Generic ---
    async def execute_query(self, sql: str, params: tuple = ()) -> list[dict]
        """Execute arbitrary SELECT query. For NL->SQL feature."""
```

### Amount Convention in DB

The DB stores amounts in **rupees** (REAL). Conversion happens at the `pine_client.py` boundary:
- DB → Pine Labs: multiply by 100 (rupees to paisa)
- Pine Labs → DB: divide by 100 (paisa to rupees)

---

## 6. Priority Scorer

### File: `scorer.py`

```python
def score_vendors(vendors: list[dict], persona_config: dict) -> list[dict]:
    """
    Score and rank vendors for payment priority.
    Returns vendors sorted by priority_score (descending) with action assigned.

    Each vendor gets:
      - priority_score: integer (higher = pay first)
      - priority_reason: plain English explanation
      - action: "pay" | "defer" | "escalate" | "queue"
    """
```

### Hospital Scoring Rules

| Factor | Score Impact |
|--------|-------------|
| `category == "critical_supply"` | +50 |
| Each overdue day | +10 per day |
| `vendor_type == "new"` | -20 |
| `vendor_type == "critical"` | +30 |
| `drug_schedule == "schedule_h"` | action = "escalate", +40 |
| `drug_schedule == "schedule_x"` | action = "queue" (blocked) |
| `is_compliant == 0` | action = "queue" (blocked) |

### Kirana Scoring Rules

| Factor | Score Impact |
|--------|-------------|
| `credit_days == 0` (COD) | +40 (pay immediately) |
| `credit_days > 0 AND days_remaining > 0` | action = "defer", float_saved = amount |
| `credit_days > 0 AND days_remaining <= 0` | +30 (credit expired, must pay) |
| `category == "dairy"` | +20 (perishable priority) |
| `vendor_type == "new"` | +10 (build relationship) |

### Credit Float Calculation

```python
def compute_credit_float(vendor: dict, invoice_date: str) -> dict:
    """
    Returns:
      - days_remaining: int (credit_days - days_since_invoice)
      - float_saved: float (amount * days_remaining / 365 * cost_of_capital)
      - should_defer: bool
    """
```

---

## 7. PRIYA MCP Server

### File: `priya_mcp_server.py`

Custom MCP server built with the `mcp` Python SDK. Runs as a subprocess, communicates via stdio JSON-RPC. This is the **single gateway** for all domain operations — every tool call does the trifecta: **API call + DB write + WebSocket event**.

### MCP Server Setup

```python
from mcp.server import Server
from mcp.server.stdio import stdio_server

app = Server("priya-mcp-server")

@app.list_tools()
async def list_tools() -> list[Tool]:
    return [...]

@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    # Dispatch to handler functions
    ...

async def main():
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())
```

### Tool Definitions (16 tools)

#### 7.1 Authentication & Setup

| Tool | Input | Output | Side Effects |
|------|-------|--------|--------------|
| `generate_token` | `{}` | `{ access_token, expires_at }` | Calls Pine Labs auth API. Caches token. |
| `load_invoices` | `{ csv_path: str, persona: str }` | `{ vendors: [...], total_amount, count }` | Parses CSV. Upserts vendors table. Returns vendor list. |
| `get_account_balance` | `{}` | `{ balance, sufficient: bool }` | Pre-flight check. Mock returns sufficient=true. |

#### 7.2 Scoring & Policy

| Tool | Input | Output | Side Effects |
|------|-------|--------|--------------|
| `score_vendors` | `{ run_id, persona }` | `{ ranked_vendors: [{vendor_id, score, reason, action}] }` | Calls scorer.py. Writes priority to orders table. Emits AGENT_NARRATION per vendor. |
| `request_policy_approval` | `{ run_id, vendors_summary: [...] }` | `{ approved: bool, approved_at }` | Emits CANVAS_STATE:policy_gate + POLICY_GATE event. **BLOCKS** until POST /approve/{run_id}. |
| `request_escalation_decision` | `{ vendor_id, flag_type, details }` | `{ decision: "capture" \| "cancel" }` | Emits ESCALATION event. **BLOCKS** until frontend responds. |

#### 7.3 Pine Labs Operations

| Tool | Input | Output | Side Effects |
|------|-------|--------|--------------|
| `create_order` | `{ run_id, vendor_id, amount, pre_auth }` | `{ pine_order_id, status, merchant_order_reference }` | Calls pine_client. Inserts orders row. Emits PIPELINE_STEP + VENDOR_STATE. |
| `create_payment` | `{ order_id, rail, amount }` | `{ pine_payment_id, status, attempt_number }` | Calls pine_client (rail-specific). Inserts payments row. Emits VENDOR_STATE. |
| `switch_rail_and_retry` | `{ order_id, failed_rail, failure_reason }` | `{ new_rail, pine_payment_id, status }` | Determines next rail. Creates new payment. Emits RAIL_SWITCH. |
| `capture_order` | `{ order_id, amount }` | `{ status }` | Calls pine_client.capture. Updates order status. |
| `cancel_order` | `{ order_id }` | `{ status }` | Calls pine_client.cancel. Updates order status. |
| `create_payment_link` | `{ order_id, amount, description }` | `{ payment_link, payment_link_id }` | Last resort fallback. Inserts payment row with rail="payment_link". |

#### 7.4 Settlement & Reconciliation

| Tool | Input | Output | Side Effects |
|------|-------|--------|--------------|
| `run_settlements` | `{ run_id, start_date, end_date }` | `{ settlements: [...], matched_count }` | Calls pine_client.get_all_settlements. Matches to orders. Writes settlements table. |
| `run_reconciliation` | `{ run_id }` | `{ reconciliations: [...], summary }` | Cross-checks orders vs settlements. Computes variance, MDR drift. Writes reconciliations table. Emits CANVAS_STATE:audit. |

#### 7.5 Queries & Events

| Tool | Input | Output | Side Effects |
|------|-------|--------|--------------|
| `execute_sql_query` | `{ sql: str }` | `{ columns, rows, row_count }` | Runs SELECT on SQLite. Emits QUERY_RESULT event. |
| `emit_event` | `{ event_type, payload }` | `{ sent: true }` | Direct WebSocket event emission. Used for AGENT_NARRATION. |
| `finalize_run` | `{ run_id }` | `{ paid, deferred, failed, float_saved, total }` | Computes final metrics. Updates runs table. Emits RUN_SUMMARY. |

### Rail Switch Logic (inside `switch_rail_and_retry`)

```
Preferred Rail → Fallback Chain:
  neft       → upi_intent → hosted_checkout → payment_link
  upi        → upi_intent → hosted_checkout → payment_link
  upi_intent → hosted_checkout → payment_link
  hosted_checkout → payment_link

Max attempts per order: 3
After all rails exhausted: create_payment_link() as last resort
```

### Blocking Tools (Policy Gate / Escalation)

These tools **suspend the MCP tool call** until an external signal arrives:

```python
# Shared state between MCP server and FastAPI
approval_events: dict[str, asyncio.Event] = {}
approval_results: dict[str, dict] = {}

async def request_policy_approval(run_id, vendors_summary):
    # 1. Emit POLICY_GATE WebSocket event
    await emit_ws("POLICY_GATE", {"run_id": run_id, "vendors": vendors_summary})
    await emit_ws("CANVAS_STATE", {"state": "policy_gate"})

    # 2. Create and wait on event
    event = asyncio.Event()
    approval_events[run_id] = event
    await event.wait()  # BLOCKS HERE

    # 3. Return result
    return approval_results.pop(run_id)
```

FastAPI side:
```python
@app.post("/approve/{run_id}")
async def approve_run(run_id: str):
    approval_results[run_id] = {"approved": True, "approved_at": datetime.utcnow().isoformat()}
    approval_events[run_id].set()  # Unblocks the MCP tool
```

---

## 8. Claude Agent SDK Orchestrator

### File: `agent.py`

This is the brain. It uses `claude_agent_sdk.query()` to create an autonomous agent that calls MCP tools to orchestrate the full pipeline.

### System Prompt (built dynamically per session)

```python
def build_system_prompt(persona: str, persona_config: dict) -> str:
    return f"""
You are PRIYA, an autonomous B2B payment orchestration agent.
Persona: {persona}
Persona rules:
{yaml.dump(persona_config)}

## Pine Labs flow you must follow:
1. Load invoices from the provided CSV using load_invoices tool
2. Generate Pine Labs auth token using generate_token tool
3. Score and rank vendors using score_vendors tool
4. Request policy approval using request_policy_approval — WAIT for approval
5. For each vendor (in priority order, based on action):
   - If action == "pay": create_order → create_payment
   - If action == "escalate": create_order with pre_auth=true → request_escalation_decision → capture or cancel
   - If action == "defer": skip payment, record deferral reason
   - If action == "queue": skip, mark as queued
6. On payment failure: use switch_rail_and_retry
7. After all vendors processed: run_settlements → run_reconciliation
8. Finalize with finalize_run

## Rules:
- ALWAYS emit_event with AGENT_NARRATION for every decision, explaining your reasoning in plain English
- ALWAYS write to DB at every state transition (tools handle this automatically)
- Amounts are in RUPEES when you pass them to tools. Tools convert to paisa internally.
- merchant_order_reference format: {{run_id}}_{{vendor_id}}
- merchant_payment_reference: fresh UUID v4 per attempt
- pre_auth=true ONLY for Schedule H flagged vendors
- Settlement is READ-ONLY. Never create/modify settlements on Pine Labs.
- For NL queries from user: use execute_sql_query with proper SQL

## DB Schema:
Tables: vendors, runs, orders, payments, settlements, reconciliations
{DB_SCHEMA_TEXT}

## Available canvas states:
workflow | policy_gate | run_board | audit | query_result
Emit CANVAS_STATE at appropriate transitions.
"""
```

### Agent Session

```python
import asyncio
from claude_agent_sdk import query, ClaudeAgentOptions

class PriyaAgentSession:
    def __init__(self, run_id: str, persona: str, instruction: str, csv_path: str):
        self.run_id = run_id
        self.persona = persona
        self.instruction = instruction
        self.csv_path = csv_path
        self.session_id = None

    async def execute(self, ws_broadcast: Callable):
        persona_config = load_persona_yaml(self.persona)
        system_prompt = build_system_prompt(self.persona, persona_config)

        prompt = f"""
Run ID: {self.run_id}
Persona: {self.persona}
Invoice file: {self.csv_path}
Instruction: {self.instruction}

Execute the full Pine Labs payment pipeline now. Start by loading invoices.
"""

        async for message in query(
            prompt=prompt,
            options=ClaudeAgentOptions(
                system_prompt=system_prompt,
                allowed_tools=[
                    "Read", "Bash", "Glob",
                    "mcp__priya__generate_token",
                    "mcp__priya__load_invoices",
                    "mcp__priya__get_account_balance",
                    "mcp__priya__score_vendors",
                    "mcp__priya__request_policy_approval",
                    "mcp__priya__request_escalation_decision",
                    "mcp__priya__create_order",
                    "mcp__priya__create_payment",
                    "mcp__priya__switch_rail_and_retry",
                    "mcp__priya__capture_order",
                    "mcp__priya__cancel_order",
                    "mcp__priya__create_payment_link",
                    "mcp__priya__run_settlements",
                    "mcp__priya__run_reconciliation",
                    "mcp__priya__execute_sql_query",
                    "mcp__priya__emit_event",
                    "mcp__priya__finalize_run",
                ],
                mcp_servers={
                    "priya": {
                        "command": "python",
                        "args": ["priya_mcp_server.py"],
                        "env": {
                            "PINE_BASE_URL": os.getenv("PINE_BASE_URL"),
                            "PINE_CLIENT_ID": os.getenv("PINE_CLIENT_ID"),
                            "PINE_CLIENT_SECRET": os.getenv("PINE_CLIENT_SECRET"),
                            "PINE_MID": os.getenv("PINE_MID"),
                            "DB_PATH": os.getenv("DB_PATH", "./priya.db"),
                            "WS_BROADCAST_URL": "ws://localhost:8000/ws/internal",
                        }
                    }
                },
                permission_mode="bypassPermissions",
            ),
        ):
            # Capture session ID
            if hasattr(message, "subtype") and message.subtype == "init":
                self.session_id = message.session_id

            # Forward agent messages to frontend
            await ws_broadcast(message)

    async def send_nl_query(self, question: str):
        """Resume session with a NL query."""
        async for message in query(
            prompt=f"User asks: {question}\nGenerate SQL, execute it, and return the result.",
            options=ClaudeAgentOptions(resume=self.session_id),
        ):
            yield message
```

---

## 9. FastAPI Server

### File: `main.py`

```python
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="PRIYA Backend")
```

### REST Endpoints

| Method | Path | Purpose | Request Body | Response |
|--------|------|---------|-------------|----------|
| POST | `/run` | Start a new payment run | `{ persona, instruction, csv_file }` | `{ run_id, status: "started" }` |
| POST | `/approve/{run_id}` | Approve policy gate | `{}` | `{ approved: true }` |
| POST | `/escalation/{run_id}/{vendor_id}` | Respond to Schedule H | `{ decision: "capture" \| "cancel" }` | `{ ok: true }` |
| POST | `/query/{run_id}` | Send NL query to agent | `{ question: str }` | `{ query_id }` (result via WS) |
| GET | `/runs` | List all runs | — | `[{ run_id, persona, status, ... }]` |
| GET | `/run/{run_id}` | Get run details + audit | — | `{ run, orders, payments, settlements, recon }` |
| GET | `/run/{run_id}/export` | CA audit export (CSV) | — | CSV file download |
| POST | `/upload-csv` | Upload invoice CSV | multipart file | `{ file_path, vendor_count }` |

### WebSocket Endpoint

```
WS /ws/{run_id}

Frontend connects here per run. Receives all events for that run.
Events are JSON: { type, payload, timestamp }
```

### Internal WebSocket (MCP → FastAPI)

The MCP server needs to emit events to connected frontends. Two approaches:

**Option A: HTTP callback** — MCP tool calls `httpx.post("http://localhost:8000/internal/event", json=event)` and FastAPI broadcasts to WebSocket clients.

**Option B: File-based IPC** — MCP writes events to a temp file, FastAPI watches and broadcasts.

**Recommended: Option A** — simpler, real-time.

```python
@app.post("/internal/event")
async def receive_mcp_event(event: dict):
    run_id = event.get("run_id")
    if run_id and run_id in active_connections:
        for ws in active_connections[run_id]:
            await ws.send_json(event)
```

### Session Management

```python
# Active agent sessions
active_sessions: dict[str, PriyaAgentSession] = {}

# Active WebSocket connections per run
active_connections: dict[str, list[WebSocket]] = {}

# Policy gate events (shared with MCP via HTTP)
approval_events: dict[str, asyncio.Event] = {}
```

### CORS Configuration

```python
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
```

### Startup

```python
@app.on_event("startup")
async def startup():
    db = PriyaDB()
    await db.init()  # Create tables if not exist
```

### Run Command

```bash
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

---

## 10. Mock Pine Labs Server

### File: `mock_pine.py`

Standalone FastAPI app mimicking Pine Labs UAT API. Runs on port 9100. Used when `PINE_MOCK=true`.

```bash
uvicorn mock_pine:app --host 0.0.0.0 --port 9100
```

### Controllable Behaviors

```python
# Configure which vendors/rails should fail (for demo)
MOCK_CONFIG = {
    "fail_rules": [
        {"vendor_name_contains": "Surgical", "rail": "neft", "fail": True},
    ],
    "settlement_delay_seconds": 2,
    "webhook_delay_seconds": 1,
}
```

### Mock Endpoints

All endpoints mirror Pine Labs UAT with deterministic responses:

- **generate_token** → Always returns valid token
- **create_order** → Returns order_id with incrementing counter
- **create_payment** → Checks fail_rules; returns PROCESSED or FAILED accordingly
- **capture_order** → Always succeeds
- **cancel_order** → Always succeeds
- **get_order** → Returns current mock state
- **settlements** → Returns pre-configured settlement data with realistic UTR, MDR
- **payment_link** → Returns mock link URL

### Settlement Mock Data

```python
MOCK_SETTLEMENTS = [{
    "total_amount": 0.96,
    "actual_transaction_amount": 45000,
    "total_deduction_amount": 810,
    "total_transactions_count": 5,
    "last_processed_date": datetime.utcnow().isoformat(),
    "settled_date": datetime.utcnow().isoformat(),
    "utr_number": "UTR2025031400001",
    "programs": ["UPI", "NETBANKING"],
}]
```

---

## 11. Persona Configuration

### File: `personas/hospital.yaml`

```yaml
persona: hospital
display_name: "City Hospital"
description: "200-bed multi-specialty hospital procurement"

mdr_rate_contracted: 0.018  # 1.8%
cost_of_capital: 0.12       # 12% annual (for float calc)

preferred_rails:
  default: neft
  fallback_chain: [upi_intent, hosted_checkout, payment_link]

priority_rules:
  category_scores:
    critical_supply: 50
    surgical_equipment: 40
    pharma_general: 20
    consumables: 10
    standard: 0
  overdue_bonus_per_day: 10
  vendor_type_scores:
    critical: 30
    established: 0
    new: -20
  max_score: 100

compliance:
  schedule_h:
    action: escalate
    pre_auth: true
    description: "Schedule H drug — requires human authorization before payment"
  schedule_x:
    action: queue
    description: "Schedule X drug — blocked, requires special license verification"
  non_compliant:
    action: queue
    description: "Vendor compliance flag inactive — payment blocked"

payment_rules:
  max_amount_per_vendor: 500000   # Rs 5 lakh
  require_approval_above: 100000  # Rs 1 lakh needs policy gate
```

### File: `personas/kirana.yaml`

```yaml
persona: kirana
display_name: "Sharma General Store"
description: "Neighborhood kirana store managing supplier payments"

mdr_rate_contracted: 0.015  # 1.5%
cost_of_capital: 0.15       # 15% annual

preferred_rails:
  default: upi
  fallback_chain: [upi_intent, payment_link]

priority_rules:
  category_scores:
    dairy: 20          # Perishable, pay first
    goods: 10
    beverages: 5
    standard: 0
  credit_rules:
    cod_bonus: 40                # COD vendors get priority
    expired_credit_bonus: 30     # Credit expired, must pay
    defer_if_credit_remaining: true
  vendor_type_scores:
    critical: 20
    established: 0
    new: 10   # Build relationship
  max_score: 100

compliance: {}  # No drug scheduling for kirana

payment_rules:
  max_amount_per_vendor: 100000   # Rs 1 lakh
  require_approval_above: 50000   # Rs 50k needs policy gate

float_optimization:
  enabled: true
  description: "Defer payments where credit period has days remaining"
  metric_name: "float_saved"
```

---

## 12. Invoice Data

### File: `invoices/hospital_invoices.csv`

```csv
vendor_id,vendor_name,category,amount,upi_id,bank_account,ifsc,preferred_rail,vendor_type,drug_schedule,is_compliant,credit_days,invoice_date,due_date
vnd_insulin_depot_001,Insulin Depot,critical_supply,45000,insulindepot@okhdfc,XXXX1234,HDFC0001234,neft,critical,,1,0,2025-01-12,2025-01-12
vnd_medchem_pharma_002,MedChem Pharma,pharma_general,32000,medchem@oksbi,XXXX5678,SBIN0005678,neft,established,schedule_h,1,0,2025-01-10,2025-01-10
vnd_surgical_supplies_003,Surgical Supplies Co,surgical_equipment,28000,surgical@okaxis,XXXX9012,UTIB0009012,neft,established,,1,0,2025-01-11,2025-01-11
vnd_bandage_world_004,Bandage World,consumables,8500,,XXXX3456,ICIC0003456,neft,new,,1,0,2025-01-13,2025-01-13
vnd_spice_house_005,Spice House Catering,standard,12000,spicehouse@okicici,,ICIC0007890,upi,established,,1,0,2025-01-13,2025-01-13
vnd_lab_reagents_006,Lab Reagents Inc,critical_supply,18500,labreagents@okhdfc,XXXX7890,HDFC0007890,neft,critical,,1,0,2025-01-09,2025-01-09
```

### File: `invoices/kirana_invoices.csv`

```csv
vendor_id,vendor_name,category,amount,upi_id,bank_account,ifsc,preferred_rail,vendor_type,drug_schedule,is_compliant,credit_days,invoice_date,due_date
vnd_sharma_wholesale_001,Sharma Wholesale,goods,28000,sharmawhole@okhdfc,XXXX1111,HDFC0001111,upi,established,,1,15,2025-01-06,2025-01-21
vnd_delhi_dairy_002,Delhi Dairy Co,dairy,12000,delhidairy@oksbi,XXXX2222,SBIN0002222,upi,critical,,1,0,2025-01-14,2025-01-14
vnd_gupta_beverages_003,Gupta Beverages,beverages,9500,guptabev@okaxis,,UTIB0003333,upi,established,,1,7,2025-01-10,2025-01-17
vnd_patel_snacks_004,Patel Snacks,goods,15000,patelsnacks@okicici,XXXX4444,ICIC0004444,upi,new,,1,0,2025-01-14,2025-01-14
vnd_fresh_produce_005,Fresh Produce Farm,dairy,8200,freshproduce@okhdfc,,HDFC0005555,upi,established,,1,0,2025-01-14,2025-01-14
vnd_cleaning_depot_006,Cleaning Supplies Depot,standard,4800,cleaningdepot@oksbi,XXXX6666,SBIN0006666,upi,established,,1,30,2024-12-20,2025-01-19
```

---

## 13. WebSocket Event Contract

All events sent from backend to frontend via WebSocket. Frontend renders based on `type`.

### Event Types

```typescript
// 1. Canvas state control — drives which view the left panel renders
{ type: "CANVAS_STATE", payload: { state: "workflow" | "policy_gate" | "run_board" | "audit" | "query_result" }, run_id, timestamp }

// 2. Agent reasoning — streams into chat panel
{ type: "AGENT_NARRATION", payload: { text: string, level: "info" | "warn" | "error" }, run_id, timestamp }

// 3. Pipeline progress — updates top bar strip
{ type: "PIPELINE_STEP", payload: { stage: "order" | "pay" | "settle" | "recon", vendor_id: string, status: string }, run_id, timestamp }

// 4. Individual vendor state — animates run board rows
{ type: "VENDOR_STATE", payload: { vendor_id, name, state, rail, amount, pine_order_id, attempt_number }, run_id, timestamp }

// 5. Policy gate — renders approve overlay
{ type: "POLICY_GATE", payload: { vendors: [{ vendor_id, name, amount, rail, priority_reason }], total: number }, run_id, timestamp }

// 6. Schedule H escalation — pulses row red
{ type: "ESCALATION", payload: { vendor_id, name, flag_type: "schedule_h", action_required: true, details: string }, run_id, timestamp }

// 7. Rail switch — shows old→new rail
{ type: "RAIL_SWITCH", payload: { vendor_id, name, from_rail, to_rail, reason, attempt_number }, run_id, timestamp }

// 8. Run complete summary
{ type: "RUN_SUMMARY", payload: { paid: number, deferred: number, failed: number, float_saved: number, total: number, vendors_processed: number }, run_id, timestamp }

// 9. NL query result
{ type: "QUERY_RESULT", payload: { query_nl: string, sql: string, columns: string[], rows: any[][], row_count: number, render_as: "table" | "bar_chart" | "summary_card" }, run_id, timestamp }
```

### Event Sequence — Typical Hospital Run

```
1.  CANVAS_STATE  { state: "workflow" }
2.  AGENT_NARRATION  "Loaded hospital persona. 6 invoices fetched."
3.  AGENT_NARRATION  "Prioritizing: Insulin Depot (critical, 2 days overdue) -> first."
4.  AGENT_NARRATION  "MedChem Pharma — Schedule H. pre_auth=true. Escalating."
5.  CANVAS_STATE  { state: "policy_gate" }
6.  POLICY_GATE  { vendors: [...], total: 144000 }
    --- BLOCKS until approve ---
7.  AGENT_NARRATION  "Approved. Executing payment run."
8.  CANVAS_STATE  { state: "run_board" }
9.  PIPELINE_STEP  { stage: "order", vendor_id: "vnd_insulin_depot_001", status: "CREATED" }
10. VENDOR_STATE  { vendor_id: "vnd_insulin_depot_001", state: "CREATED", rail: "neft", ... }
11. PIPELINE_STEP  { stage: "pay", vendor_id: "vnd_insulin_depot_001", status: "PROCESSED" }
12. VENDOR_STATE  { vendor_id: "vnd_insulin_depot_001", state: "PROCESSED", ... }
13. AGENT_NARRATION  "Insulin Depot — pine_ord_abc → PROCESSED"
14. ESCALATION  { vendor_id: "vnd_medchem_pharma_002", flag_type: "schedule_h", ... }
    --- BLOCKS until capture/cancel ---
15. PIPELINE_STEP  { stage: "pay", vendor_id: "vnd_surgical_supplies_003", status: "FAILED" }
16. RAIL_SWITCH  { vendor_id: "vnd_surgical_supplies_003", from_rail: "neft", to_rail: "upi_intent", reason: "Bank node down" }
17. VENDOR_STATE  { vendor_id: "vnd_surgical_supplies_003", state: "PROCESSED", rail: "upi_intent", attempt_number: 2 }
18. AGENT_NARRATION  "Surgical Supplies — NEFT FAILED. Switched to UPI intent. PROCESSED."
    ... (more vendors) ...
19. CANVAS_STATE  { state: "audit" }
20. AGENT_NARRATION  "Settlement: UTR2025011400001 matched. MDR Rs 11 on Spice House."
21. AGENT_NARRATION  "Recon complete. 4/5 paid. 1 escalated. Audit ready."
22. RUN_SUMMARY  { paid: 4, deferred: 0, failed: 0, float_saved: 0, total: 144000, ... }
```

---

## 14. Environment & Configuration

### File: `.env`

```bash
# Pine Labs API
PINE_BASE_URL=https://pluraluat.v2.pinepg.in/api
PINE_MID=121495
PINE_CLIENT_ID=b74c51a7-da84-4f2d-b3b2-566ea676fc64
PINE_CLIENT_SECRET=<secret>
PINE_MOCK=false

# Claude Agent SDK
ANTHROPIC_API_KEY=<key>

# Database
DB_PATH=./priya.db

# Server
HOST=0.0.0.0
PORT=8000
FRONTEND_URL=http://localhost:3000
```

### File: `.gitignore`

```
.env
priya.db
__pycache__/
*.pyc
.venv/
node_modules/
```

---

## 15. Error Handling & Recovery

### Payment Failures

```
Attempt 1: preferred_rail → FAILED
  → Log failure_reason in payments table
  → RAIL_SWITCH event
Attempt 2: next rail in fallback_chain → FAILED
  → Log failure_reason
  → RAIL_SWITCH event
Attempt 3: next rail → FAILED
  → Create payment_link (last resort)
  → Notify via AGENT_NARRATION
  → Mark order as "queued" with recovery_action="payment_link"
  → Continue to next vendor
```

### Token Expiry

```
On 401 from Pine Labs:
  → generate_token() again
  → Retry failed request once
  → If still fails: log error, continue
```

### Agent Errors

```
If MCP tool throws exception:
  → Agent sees error in tool result
  → Agent decides: retry, skip vendor, or escalate
  → AGENT_NARRATION explains the decision
```

### Demo Safety

```
If PINE_MOCK=true:
  → All Pine Labs calls go to mock_pine.py on port 9100
  → Mock never fails unexpectedly
  → Controlled failures only per MOCK_CONFIG
```

---

## 16. Sprint Build Order

| Sprint | Files | Milestone |
|--------|-------|-----------|
| **0 — Foundation** | `priya_init.sql`, `db.py`, `personas/*.yaml`, `invoices/*.csv`, `requirements.txt`, `.env`, `.gitignore`, `CLAUDE.md` | DB initializes, persona loads, CSV parses |
| **1 — Pine Labs Client** | `pine_client.py`, `mock_pine.py`, `scorer.py` | Can auth, create orders, create payments against mock |
| **2 — MCP Server** | `priya_mcp_server.py` | All 16 tools callable via MCP protocol |
| **3 — Agent + Server** | `agent.py`, `main.py` | Hospital happy path end-to-end (no failures) |
| **4 — Recovery + Gates** | Update `priya_mcp_server.py`, `agent.py` | Policy gate blocks/resumes, Schedule H, rail switch, payment link fallback |
| **5 — Kirana + NL-SQL** | Update `scorer.py`, `priya_mcp_server.py` | Credit float scoring, NL->SQL queries, CA export endpoint |
| **6 — Polish** | Update `mock_pine.py`, `main.py` | Demo hardening, controlled failures, event sequence verified |
