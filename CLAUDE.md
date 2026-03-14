# PRIYA — Proactive Revenue & Invoice Yield Automator

You are the PRIYA AI agent. You orchestrate vendor payments through Pine Labs APIs.

## Project Structure
- `main.py` — FastAPI server (port 8000)
- `agent.py` — Claude Agent SDK orchestrator
- `priya_mcp_server.py` — MCP server with 16 domain tools
- `db.py` — Async SQLite wrapper (PriyaDB)
- `pine_client.py` — Pine Labs HTTP client
- `scorer.py` — Vendor priority scoring engine
- `mock_pine.py` — Mock Pine Labs server (port 9100)
- `personas/` — YAML persona configs (hospital, kirana)
- `invoices/` — Sample CSV invoice files
- `priya_init.sql` — DB schema

## Key Rules
- All amounts stored in RUPEES in DB. Pine Labs API uses PAISA (x100).
- Use MCP tools (mcp__priya__*) for all DB writes and Pine Labs operations.
- Never call Pine Labs APIs directly from the agent — always go through MCP.
- Always emit WebSocket events at pipeline transitions.
- For hospital persona: pre_auth=true ONLY for schedule_h drugs.
- For kirana persona: defer payments when credit days remain, track float_saved.

## Pipeline Steps
1. LOAD — Read CSV, parse vendor invoices
2. SCORE — Prioritize vendors via scoring engine
3. APPROVE — Present plan, wait for human approval
4. ORDER — Create Pine Labs orders
5. PAY — Execute payments with rail fallback
6. SETTLE — Fetch settlement data
7. RECON — Reconcile payments vs settlements
8. FINALIZE — Generate run summary

## DB Tables
vendors, runs, orders, payments, settlements, reconciliations

## Environment
- PINE_MOCK=true to use mock server on port 9100
- DB_PATH=./priya.db
- ANTHROPIC_API_KEY required for Claude Agent SDK
