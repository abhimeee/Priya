"""
agent.py — Claude Agent SDK orchestrator for PRIYA.

Wraps claude_agent_sdk.query() with persona-aware system prompts,
MCP server configuration, and WebSocket streaming.
"""
import asyncio
import json
import os
import pathlib
import uuid
from typing import Any, Callable, Coroutine, Optional

import yaml
from claude_agent_sdk import ClaudeAgentOptions, query

PERSONAS_DIR = pathlib.Path(__file__).parent / "personas"
MCP_SERVER_PATH = pathlib.Path(__file__).parent / "priya_mcp_server.py"

DB_SCHEMA_TEXT = """
Tables:
  vendors(id, name, persona, category, upi_id, bank_account, ifsc, preferred_rail, credit_days, vendor_type, drug_schedule, is_compliant, created_at)
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


def _load_persona_yaml(persona: str) -> dict:
    path = PERSONAS_DIR / f"{persona}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"Persona file not found: {path}")
    return yaml.safe_load(path.read_text())


def build_system_prompt(persona: str, persona_config: dict) -> str:
    display_name = persona_config.get("display_name", persona)
    mdr_contracted = persona_config.get("mdr_rate_contracted", "N/A")
    preferred_rails = persona_config.get("preferred_rails", {})
    priority_rules = yaml.dump(persona_config.get("priority_rules", {}), default_flow_style=False)
    compliance = yaml.dump(persona_config.get("compliance", {}), default_flow_style=False)
    payment_rules = yaml.dump(persona_config.get("payment_rules", {}), default_flow_style=False)
    float_opt = yaml.dump(persona_config.get("float_optimization", {}), default_flow_style=False)

    return f"""You are PRIYA (Proactive Revenue & Invoice Yield Automator), an AI agent that orchestrates vendor payments through the Pine Labs payment pipeline.

## Current Persona: {display_name} ({persona})
MDR contracted rate: {mdr_contracted}
Preferred rails: {yaml.dump(preferred_rails, default_flow_style=True).strip()}

### Priority Rules
{priority_rules}

### Compliance Rules
{compliance}

### Payment Rules
{payment_rules}

### Float Optimization
{float_opt}

## Pine Labs Pipeline Steps
Execute these steps IN ORDER for each vendor payment run:

1. **LOAD**: Read the CSV file and parse vendor invoices
2. **SCORE**: Score and prioritize vendors using the scoring engine (call mcp__priya__score_vendors)
3. **APPROVE**: Present the payment plan to the user for approval (call mcp__priya__request_approval). WAIT for approval before proceeding.
4. **ORDER**: For each approved vendor, create a Pine Labs order (call mcp__priya__create_pine_order)
5. **PAY**: Execute payment for each order using preferred rail with fallback chain (call mcp__priya__execute_payment)
6. **SETTLE**: After payments complete, fetch settlement data (call mcp__priya__fetch_settlements)
7. **RECON**: Reconcile payments against settlements, flag variances (call mcp__priya__reconcile_payment)
8. **FINALIZE**: Generate run summary with metrics (call mcp__priya__finalize_run)

## Rules
- ALWAYS emit AGENT_NARRATION events via mcp__priya__emit_event to narrate what you are doing
- ALWAYS write to DB via MCP tools — never skip DB writes
- All monetary amounts are in RUPEES (the MCP tools handle paisa conversion for Pine Labs)
- For hospital persona: use pre_auth=true ONLY for schedule_h drug vendors
- For kirana persona: defer vendors with remaining credit days, track float_saved
- If a payment fails, try the next rail in the fallback chain before escalating
- Emit CANVAS_STATE events at major transitions (loading, scoring, approving, paying, settling, reconciling, complete)
- Emit VENDOR_STATE events when a vendor's status changes
- Emit PIPELINE_STEP events at each pipeline stage transition
- On escalation (schedule_h drugs, compliance issues), emit ESCALATION event and wait for human decision
- Emit RAIL_SWITCH event when falling back to a different payment rail
- At the end, emit RUN_SUMMARY with aggregate metrics

## DB Schema
{DB_SCHEMA_TEXT}

## Important
- You have access to the PRIYA MCP server tools (mcp__priya__*) for all DB and pipeline operations.
- You also have Read, Bash, and Glob tools for file operations.
- Do NOT call Pine Labs APIs directly — always go through MCP tools which handle auth and conversion.
- Think step by step, narrate your reasoning, and handle errors gracefully.
"""


class PriyaAgentSession:
    """Manages a single PRIYA agent run backed by Claude Agent SDK."""

    def __init__(
        self,
        run_id: str,
        persona: str,
        instruction: str,
        csv_path: str,
    ):
        self.run_id = run_id
        self.persona = persona
        self.instruction = instruction
        self.csv_path = csv_path
        self.session_id: Optional[str] = None
        self._persona_config = _load_persona_yaml(persona)

    async def execute(
        self,
        ws_broadcast: Callable[[dict], Coroutine[Any, Any, None]],
    ) -> None:
        """Run the full PRIYA pipeline via Claude Agent SDK."""
        system_prompt = build_system_prompt(self.persona, self._persona_config)

        user_prompt = (
            f"Run ID: {self.run_id}\n"
            f"Persona: {self.persona}\n"
            f"CSV file: {self.csv_path}\n"
            f"Instruction: {self.instruction}\n\n"
            f"Execute the full PRIYA payment pipeline for this run. "
            f"Start by reading the CSV file, then proceed through all 8 steps. "
            f"Narrate each step clearly."
        )

        mcp_servers = [
            {
                "name": "priya",
                "type": "stdio",
                "command": "python",
                "args": [str(MCP_SERVER_PATH)],
                "env": {
                    "PRIYA_RUN_ID": self.run_id,
                    "PRIYA_API_BASE": os.getenv("PRIYA_API_BASE", "http://localhost:8000"),
                    "PINE_BASE_URL": os.getenv("PINE_BASE_URL", ""),
                    "PINE_MID": os.getenv("PINE_MID", ""),
                    "PINE_CLIENT_ID": os.getenv("PINE_CLIENT_ID", ""),
                    "PINE_CLIENT_SECRET": os.getenv("PINE_CLIENT_SECRET", ""),
                    "PINE_MOCK": os.getenv("PINE_MOCK", "false"),
                    "DB_PATH": os.getenv("DB_PATH", "./priya.db"),
                },
            }
        ]

        options = ClaudeAgentOptions(
            system_prompt=system_prompt,
            allowed_tools=["Read", "Bash", "Glob", "mcp__priya__*"],
            mcp_servers=mcp_servers,
            permission_mode="bypassPermissions",
        )

        try:
            async for message in query(prompt=user_prompt, options=options):
                msg_type = message.get("type", "")

                # Capture session ID from init message
                if msg_type == "init" and "session_id" in message:
                    self.session_id = message["session_id"]

                # Forward assistant text to WebSocket
                if msg_type == "assistant" and message.get("content"):
                    await ws_broadcast({
                        "type": "AGENT_MESSAGE",
                        "run_id": self.run_id,
                        "content": message["content"],
                    })

                # Forward tool results
                if msg_type == "tool_result":
                    await ws_broadcast({
                        "type": "TOOL_RESULT",
                        "run_id": self.run_id,
                        "tool": message.get("tool_name", ""),
                        "result": message.get("result", ""),
                    })

        except Exception as e:
            await ws_broadcast({
                "type": "AGENT_ERROR",
                "run_id": self.run_id,
                "error": str(e),
            })
            raise

    async def send_nl_query(
        self,
        question: str,
        ws_broadcast: Callable[[dict], Coroutine[Any, Any, None]],
    ) -> str:
        """Send a natural language query to the active session."""
        if not self.session_id:
            raise RuntimeError("No active session — run execute() first")

        query_id = str(uuid.uuid4())

        prompt = (
            f"The user asks about run {self.run_id}:\n\n"
            f"{question}\n\n"
            f"Answer using the PRIYA DB via mcp__priya__execute_query or mcp__priya__get_run_detail. "
            f"Be concise and data-driven."
        )

        options = ClaudeAgentOptions(
            session_id=self.session_id,
            allowed_tools=["mcp__priya__*"],
            permission_mode="bypassPermissions",
        )

        try:
            async for message in query(prompt=prompt, options=options):
                msg_type = message.get("type", "")

                if msg_type == "assistant" and message.get("content"):
                    await ws_broadcast({
                        "type": "QUERY_RESULT",
                        "run_id": self.run_id,
                        "query_id": query_id,
                        "content": message["content"],
                    })
        except Exception as e:
            await ws_broadcast({
                "type": "QUERY_RESULT",
                "run_id": self.run_id,
                "query_id": query_id,
                "content": f"Error: {e}",
            })

        return query_id
