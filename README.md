# PRIYA - Proactive Revenue & Invoice Yield Automator

**Pine Labs AI Hackathon 2026**

PRIYA is an autonomous AI agent that orchestrates end-to-end B2B vendor payments through the Pine Labs payment pipeline. Give it a natural language instruction like *"Pay all hospital vendors for March, defer non-critical below Rs 10K"* and it handles the entire flow — scoring, approval, payment, settlement, and reconciliation — in real time.

## How It Works

```
User: "Pay hospital vendors, prioritize critical supplies, defer consumables"
  |
  v
Claude Agent SDK (orchestrator)
  |-- Reads CSV invoices
  |-- Scores & prioritizes vendors
  |-- Requests human approval (blocks until granted)
  |-- Creates Pine Labs orders + payments per vendor
  |-- Retries with rail fallback on failure (NEFT -> UPI -> Checkout -> Link)
  |-- Fetches settlements, runs reconciliation
  |-- Emits real-time events to React UI via WebSocket
```

## Architecture

```
React Frontend (separate repo)
       |
  WebSocket + REST
       |
main.py (FastAPI :8000)
       |
agent.py (Claude Agent SDK)
       |
priya_mcp_server.py (17 MCP tools, stdio)
       |
  +----+----+----+
  |         |         |
pine_client.py  db.py    WebSocket Emitter
  |         |         |
Pine Labs API  SQLite    Frontend Events
```

**Key design**: Every MCP tool that mutates state follows the **trifecta pattern** — Pine Labs API call + DB write + WebSocket event emission.

## Personas

PRIYA supports persona-based payment strategies via YAML config:

| Persona | Business | Default Rail | MDR Rate | Special Logic |
|---------|----------|-------------|----------|---------------|
| **Hospital** | City Hospital | NEFT | 1.8% | Schedule H drug compliance, pre-auth for controlled substances, escalation gates |
| **Kirana** | Sharma General Store | UPI | 1.5% | Credit float optimization, defer vendors with remaining credit days, track float saved |

## Project Structure

```
claude-agent-sdk/
+-- main.py                  # FastAPI server (REST + WebSocket + internal gates)
+-- agent.py                 # Claude Agent SDK orchestrator
+-- priya_mcp_server.py      # 17 MCP tools (stdio JSON-RPC)
+-- pine_client.py           # Pine Labs REST API client (httpx)
+-- db.py                    # Async SQLite wrapper (aiosqlite)
+-- scorer.py                # Vendor priority scoring engine
+-- mock_pine.py             # Mock Pine Labs server for testing (:9100)
+-- priya_init.sql           # DB schema (6 tables + 13 indexes)
+-- requirements.txt         # Python dependencies
+-- .env                     # Environment config (not committed)
+-- CLAUDE.md                # Agent context file
+-- personas/
|   +-- hospital.yaml        # Hospital persona config
|   +-- kirana.yaml          # Kirana persona config
+-- invoices/
|   +-- hospital_invoices.csv
|   +-- kirana_invoices.csv
+-- final-docs/
    +-- BACKEND_SPEC.md      # Full backend technical specification
    +-- UI_HANDOFF.md        # Frontend team handoff document
```

## Quick Start

### Prerequisites

- Python 3.11+
- An [Anthropic API key](https://console.anthropic.com/)
- Pine Labs UAT credentials (MID, client_id, client_secret)

### Setup

```bash
# Clone and enter the project
git clone https://github.com/abhimeee/Priya.git
cd Priya
git checkout claude-agent-sdk

# Create virtual environment
python -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Configure environment
cp .env.example .env  # or create .env manually
```

### Environment Variables

```env
ANTHROPIC_API_KEY=your-key-here
PINE_BASE_URL=https://pluraluat.v2.pinepg.in/api
PINE_MID=121495
PINE_CLIENT_ID=your-client-id
PINE_CLIENT_SECRET=your-client-secret
PINE_MOCK=true          # Set to true for local testing with mock server
DB_PATH=./priya.db
```

### Run (Mock Mode)

```bash
# Terminal 1: Start mock Pine Labs server
uvicorn mock_pine:app --port 9100

# Terminal 2: Start PRIYA backend
PINE_MOCK=true uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

### Run (Live Pine Labs UAT)

```bash
PINE_MOCK=false uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

## API Endpoints

### REST

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/run` | Start a new payment run |
| `POST` | `/approve/{run_id}` | Approve a pending payment plan |
| `POST` | `/escalation/{run_id}/{vendor_id}` | Resolve an escalation (approve/reject/override) |
| `POST` | `/query/{run_id}` | Ask a natural language question about a run |
| `GET` | `/runs` | List all runs |
| `GET` | `/run/{run_id}` | Get full run details (orders, payments, settlements, recon) |
| `GET` | `/run/{run_id}/export` | Export reconciliation as CSV |
| `POST` | `/upload-csv` | Upload a vendor invoice CSV |

### WebSocket

Connect to `ws://localhost:8000/ws/{run_id}` for real-time events:

| Event | Description |
|-------|-------------|
| `CANVAS_STATE` | UI state transitions (loading, scoring, paying, audit) |
| `AGENT_NARRATION` | Agent's reasoning and narration text |
| `PIPELINE_STEP` | Pipeline stage transitions |
| `VENDOR_STATE` | Individual vendor status updates |
| `POLICY_GATE` | Approval request (blocks pipeline) |
| `ESCALATION` | Escalation request for compliance issues |
| `RAIL_SWITCH` | Payment rail fallback notification |
| `RUN_SUMMARY` | Final run metrics |
| `QUERY_RESULT` | NL query response |

## Example: Start a Hospital Run

```bash
# Start a run
curl -X POST http://localhost:8000/run \
  -H "Content-Type: application/json" \
  -d '{
    "persona": "hospital",
    "instruction": "Pay all vendors for March. Prioritize critical supplies. Escalate Schedule H drugs.",
    "csv_file": "invoices/hospital_invoices.csv"
  }'

# Approve the payment plan (after POLICY_GATE event)
curl -X POST http://localhost:8000/approve/{run_id}

# Ask a question about the run
curl -X POST http://localhost:8000/query/{run_id} \
  -H "Content-Type: application/json" \
  -d '{"question": "Which vendors were deferred and why?"}'
```

## MCP Tools (17)

The PRIYA MCP server exposes these tools to the Claude agent:

| Tool | Purpose |
|------|---------|
| `generate_token` | Authenticate with Pine Labs |
| `load_invoices` | Parse CSV and upsert vendors |
| `get_account_balance` | Check available balance |
| `score_vendors` | Priority scoring with persona rules |
| `request_policy_approval` | Block until human approves payment plan |
| `request_escalation_decision` | Block for compliance escalations |
| `create_order` | Create Pine Labs order |
| `create_payment` | Execute payment on preferred rail |
| `switch_rail_and_retry` | Retry failed payment on next rail |
| `capture_order` | Capture a pre-authorized order |
| `cancel_order` | Cancel an order |
| `create_payment_link` | Generate a payment link |
| `run_settlements` | Fetch and store settlement data |
| `run_reconciliation` | Cross-check payments vs settlements |
| `execute_sql_query` | Run read-only SQL (NL->SQL) |
| `emit_event` | Push WebSocket event to frontend |
| `finalize_run` | Compute final metrics and close run |

## Database Schema

6 tables tracking the full payment lifecycle:

- **vendors** - Vendor master data (UPI, bank, compliance flags)
- **runs** - Payment run metadata and aggregate metrics
- **orders** - Pine Labs orders linked to vendors
- **payments** - Payment attempts with rail, status, retries
- **settlements** - Settlement records with UTR, fees, deductions
- **reconciliations** - Full audit trail with variance analysis and agent reasoning

## Tech Stack

- **Claude Agent SDK** (`claude_agent_sdk`) - AI orchestration
- **FastAPI** + **uvicorn** - REST API + WebSocket server
- **aiosqlite** - Async SQLite persistence
- **httpx** - Async HTTP client for Pine Labs API
- **MCP** (Model Context Protocol) - Tool interface between agent and backend
- **PyYAML** - Persona configuration
- **Pydantic** - Request/response validation

## Documentation

- [`final-docs/BACKEND_SPEC.md`](final-docs/BACKEND_SPEC.md) - Complete backend technical specification
- [`final-docs/UI_HANDOFF.md`](final-docs/UI_HANDOFF.md) - Frontend team handoff with TypeScript types, WebSocket contract, component tree, and wireframes

## Team

Built for the **Pine Labs AI Hackathon 2026**.

---

*PRIYA: Because paying vendors shouldn't require a spreadsheet and three phone calls.*
