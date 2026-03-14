"""
PRIYA async SQLite wrapper using aiosqlite.
Amounts are stored in RUPEES (REAL). Paisa conversion happens at pine_client boundary.
"""
import os
import pathlib
import aiosqlite
from datetime import datetime
from typing import Any, Optional


DB_PATH = os.getenv("DB_PATH", "./priya.db")
SQL_INIT = pathlib.Path(__file__).parent / "priya_init.sql"


class PriyaDB:
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._db: Optional[aiosqlite.Connection] = None

    async def init(self) -> None:
        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA foreign_keys = ON")
        sql = SQL_INIT.read_text()
        await self._db.executescript(sql)
        await self._db.commit()
        # Migrate existing reconciliations tables to add new columns (idempotent)
        _new_recon_cols = [
            ("checks", "TEXT"),
            ("recon_status", "TEXT DEFAULT 'PENDING'"),
            ("bank_credit_amount", "REAL"),
            ("bank_delta", "REAL"),
            ("settlement_delay_days", "INTEGER"),
        ]
        for col, typedef in _new_recon_cols:
            try:
                await self._db.execute(
                    f"ALTER TABLE reconciliations ADD COLUMN {col} {typedef}"
                )
                await self._db.commit()
            except Exception:
                pass  # Column already exists — safe to ignore

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    # ------------------------------------------------------------------ helpers
    def _now(self) -> str:
        return datetime.utcnow().isoformat()

    async def _fetchone(self, sql: str, params: tuple = ()) -> Optional[dict]:
        async with self._db.execute(sql, params) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def _fetchall(self, sql: str, params: tuple = ()) -> list[dict]:
        async with self._db.execute(sql, params) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    # ------------------------------------------------------------------ vendors
    async def upsert_vendor(self, vendor: dict) -> None:
        defaults = {
            "id": None, "name": None, "persona": None, "category": None,
            "upi_id": None, "bank_account": None, "ifsc": None,
            "preferred_rail": "upi", "credit_days": 0, "vendor_type": "established",
            "drug_schedule": None, "is_compliant": 1,
            "amount": 0, "invoice_number": None, "invoice_date": None,
            "due_date": None, "items": 0,
            "priority_score": 0, "priority_reason": "", "action": "pay",
            "created_at": self._now(),
        }
        row = {**defaults, **vendor}
        await self._db.execute(
            """
            INSERT INTO vendors (
                id, name, persona, category, upi_id, bank_account, ifsc,
                preferred_rail, credit_days, vendor_type, drug_schedule,
                is_compliant, amount, invoice_number, invoice_date, due_date,
                items, priority_score, priority_reason, action, created_at
            ) VALUES (
                :id, :name, :persona, :category, :upi_id, :bank_account, :ifsc,
                :preferred_rail, :credit_days, :vendor_type, :drug_schedule,
                :is_compliant, :amount, :invoice_number, :invoice_date, :due_date,
                :items, :priority_score, :priority_reason, :action, :created_at
            )
            ON CONFLICT(id) DO UPDATE SET
                name=excluded.name, persona=excluded.persona,
                category=excluded.category, upi_id=excluded.upi_id,
                bank_account=excluded.bank_account, ifsc=excluded.ifsc,
                preferred_rail=excluded.preferred_rail,
                credit_days=excluded.credit_days,
                vendor_type=excluded.vendor_type,
                drug_schedule=excluded.drug_schedule,
                is_compliant=excluded.is_compliant,
                amount=excluded.amount,
                invoice_number=excluded.invoice_number,
                invoice_date=excluded.invoice_date,
                due_date=excluded.due_date,
                items=excluded.items,
                priority_score=excluded.priority_score,
                priority_reason=excluded.priority_reason,
                action=excluded.action
            """,
            row,
        )
        await self._db.commit()

    async def get_vendor(self, vendor_id: str) -> Optional[dict]:
        return await self._fetchone("SELECT * FROM vendors WHERE id = ?", (vendor_id,))

    async def get_vendors_by_persona(self, persona: str) -> list[dict]:
        return await self._fetchall(
            "SELECT * FROM vendors WHERE persona = ? ORDER BY name", (persona,)
        )

    # ------------------------------------------------------------------ runs
    async def create_run(self, run: dict) -> None:
        defaults = {
            "id": None, "persona": None, "instruction": None,
            "invoice_source": None, "pine_token": "", "status": "pending",
            "total_vendors": 0, "total_amount": 0, "paid_amount": 0,
            "deferred_amount": 0, "float_saved": 0,
            "started_at": self._now(), "completed_at": None, "approved_at": None,
        }
        row = {**defaults, **run}
        await self._db.execute(
            """
            INSERT INTO runs (
                id, persona, instruction, invoice_source, pine_token, status,
                total_vendors, total_amount, paid_amount, deferred_amount,
                float_saved, started_at, completed_at, approved_at
            ) VALUES (
                :id, :persona, :instruction, :invoice_source, :pine_token, :status,
                :total_vendors, :total_amount, :paid_amount, :deferred_amount,
                :float_saved, :started_at, :completed_at, :approved_at
            )
            """,
            row,
        )
        await self._db.commit()

    async def update_run_status(self, run_id: str, status: str, **kwargs: Any) -> None:
        fields = {"status": status}
        fields.update(kwargs)
        if status in ("completed", "failed") and "completed_at" not in fields:
            fields["completed_at"] = self._now()
        set_clause = ", ".join(f"{k} = :{k}" for k in fields)
        fields["_id"] = run_id
        await self._db.execute(
            f"UPDATE runs SET {set_clause} WHERE id = :_id", fields
        )
        await self._db.commit()

    async def get_run(self, run_id: str) -> Optional[dict]:
        return await self._fetchone("SELECT * FROM runs WHERE id = ?", (run_id,))

    # ------------------------------------------------------------------ orders
    async def insert_order(self, order: dict) -> None:
        defaults = {
            "id": None, "run_id": None, "vendor_id": None,
            "pine_order_id": "", "merchant_order_reference": "",
            "amount": 0, "priority_score": 0, "priority_reason": "",
            "action": "pay", "pre_auth": 0, "pine_status": "CREATED",
            "escalation_flag": None, "defer_reason": None,
            "created_at": self._now(), "updated_at": self._now(),
        }
        row = {**defaults, **order}
        await self._db.execute(
            """
            INSERT INTO orders (
                id, run_id, vendor_id, pine_order_id, merchant_order_reference,
                amount, priority_score, priority_reason, action, pre_auth,
                pine_status, escalation_flag, defer_reason, created_at, updated_at
            ) VALUES (
                :id, :run_id, :vendor_id, :pine_order_id, :merchant_order_reference,
                :amount, :priority_score, :priority_reason, :action, :pre_auth,
                :pine_status, :escalation_flag, :defer_reason, :created_at, :updated_at
            )
            """,
            row,
        )
        await self._db.commit()

    async def update_order_status(
        self, order_id: str, pine_status: str, **kwargs: Any
    ) -> None:
        fields = {"pine_status": pine_status, "updated_at": self._now()}
        fields.update(kwargs)
        set_clause = ", ".join(f"{k} = :{k}" for k in fields)
        fields["_id"] = order_id
        await self._db.execute(
            f"UPDATE orders SET {set_clause} WHERE id = :_id", fields
        )
        await self._db.commit()

    async def get_orders_by_run(self, run_id: str) -> list[dict]:
        return await self._fetchall(
            "SELECT * FROM orders WHERE run_id = ? ORDER BY priority_score DESC",
            (run_id,),
        )

    async def get_order_by_pine_id(self, pine_order_id: str) -> Optional[dict]:
        return await self._fetchone(
            "SELECT * FROM orders WHERE pine_order_id = ?", (pine_order_id,)
        )

    # ------------------------------------------------------------------ payments
    async def insert_payment(self, payment: dict) -> None:
        defaults = {
            "id": None, "run_id": None, "order_id": None, "vendor_id": None,
            "pine_order_id": "", "pine_payment_id": None,
            "merchant_payment_reference": "", "amount": 0, "rail": "upi",
            "attempt_number": 1, "pine_status": "PENDING",
            "failure_reason": None, "recovery_action": None, "webhook_event": None,
            "request_id": "", "initiated_at": self._now(), "confirmed_at": None,
        }
        row = {**defaults, **payment}
        await self._db.execute(
            """
            INSERT INTO payments (
                id, run_id, order_id, vendor_id, pine_order_id, pine_payment_id,
                merchant_payment_reference, amount, rail, attempt_number,
                pine_status, failure_reason, recovery_action, webhook_event,
                request_id, initiated_at, confirmed_at
            ) VALUES (
                :id, :run_id, :order_id, :vendor_id, :pine_order_id, :pine_payment_id,
                :merchant_payment_reference, :amount, :rail, :attempt_number,
                :pine_status, :failure_reason, :recovery_action, :webhook_event,
                :request_id, :initiated_at, :confirmed_at
            )
            """,
            row,
        )
        await self._db.commit()

    async def update_payment_status(
        self, payment_id: str, pine_status: str, **kwargs: Any
    ) -> None:
        fields = {"pine_status": pine_status}
        fields.update(kwargs)
        if pine_status in ("SUCCESS", "PROCESSED") and "confirmed_at" not in fields:
            fields["confirmed_at"] = self._now()
        set_clause = ", ".join(f"{k} = :{k}" for k in fields)
        fields["_id"] = payment_id
        await self._db.execute(
            f"UPDATE payments SET {set_clause} WHERE id = :_id", fields
        )
        await self._db.commit()

    async def get_payments_by_order(self, order_id: str) -> list[dict]:
        return await self._fetchall(
            "SELECT * FROM payments WHERE order_id = ? ORDER BY attempt_number",
            (order_id,),
        )

    async def get_payments_by_pine_order(self, pine_order_id: str) -> list[dict]:
        return await self._fetchall(
            "SELECT * FROM payments WHERE pine_order_id = ? ORDER BY attempt_number",
            (pine_order_id,),
        )

    # ------------------------------------------------------------------ settlements
    async def insert_settlement(self, settlement: dict) -> None:
        defaults = {
            "id": None, "run_id": None, "pine_order_id": "",
            "pine_settlement_id": None, "utr_number": "", "bank_account": None,
            "last_processed_date": self._now(), "expected_amount": 0,
            "settled_amount": 0, "platform_fee": 0, "total_deduction_amount": 0,
            "refund_debit": 0, "fee_flagged": 0, "status": "pending",
            "settled_at": None, "created_at": self._now(),
        }
        row = {**defaults, **settlement}
        await self._db.execute(
            """
            INSERT INTO settlements (
                id, run_id, pine_order_id, pine_settlement_id, utr_number,
                bank_account, last_processed_date, expected_amount, settled_amount,
                platform_fee, total_deduction_amount, refund_debit, fee_flagged,
                status, settled_at, created_at
            ) VALUES (
                :id, :run_id, :pine_order_id, :pine_settlement_id, :utr_number,
                :bank_account, :last_processed_date, :expected_amount, :settled_amount,
                :platform_fee, :total_deduction_amount, :refund_debit, :fee_flagged,
                :status, :settled_at, :created_at
            )
            """,
            row,
        )
        await self._db.commit()

    async def get_settlements_by_run(self, run_id: str) -> list[dict]:
        return await self._fetchall(
            "SELECT * FROM settlements WHERE run_id = ? ORDER BY created_at",
            (run_id,),
        )

    async def get_settlements_by_utr(self, utr_number: str) -> list[dict]:
        return await self._fetchall(
            "SELECT * FROM settlements WHERE utr_number = ? ORDER BY created_at",
            (utr_number,),
        )

    async def get_reconciliations_by_utr(self, run_id: str, utr_number: str) -> list[dict]:
        return await self._fetchall(
            "SELECT * FROM reconciliations WHERE run_id = ? AND utr_number = ? ORDER BY created_at",
            (run_id, utr_number),
        )

    # ------------------------------------------------------------------ reconciliations
    async def insert_reconciliation(self, recon: dict) -> None:
        defaults = {
            "id": None, "run_id": None, "order_id": None, "payment_id": None,
            "settlement_id": None, "vendor_id": None, "pine_order_id": "",
            "merchant_order_reference": "", "utr_number": None, "persona": "",
            "invoice_amount": 0, "paid_amount": 0, "settled_amount": 0,
            "variance": 0, "mdr_rate_actual": None, "mdr_rate_contracted": None,
            "mdr_drift_flagged": 0, "rail_used": None, "retries": 0,
            "outcome": "pending", "pre_auth_used": 0, "agent_reasoning": "",
            "ca_notes": None, "created_at": self._now(),
            "checks": None, "recon_status": "PENDING",
            "bank_credit_amount": None, "bank_delta": None,
            "settlement_delay_days": None,
        }
        row = {**defaults, **recon}
        await self._db.execute(
            """
            INSERT INTO reconciliations (
                id, run_id, order_id, payment_id, settlement_id, vendor_id,
                pine_order_id, merchant_order_reference, utr_number, persona,
                invoice_amount, paid_amount, settled_amount, variance,
                mdr_rate_actual, mdr_rate_contracted, mdr_drift_flagged,
                rail_used, retries, outcome, pre_auth_used, agent_reasoning,
                ca_notes, created_at, checks, recon_status, bank_credit_amount,
                bank_delta, settlement_delay_days
            ) VALUES (
                :id, :run_id, :order_id, :payment_id, :settlement_id, :vendor_id,
                :pine_order_id, :merchant_order_reference, :utr_number, :persona,
                :invoice_amount, :paid_amount, :settled_amount, :variance,
                :mdr_rate_actual, :mdr_rate_contracted, :mdr_drift_flagged,
                :rail_used, :retries, :outcome, :pre_auth_used, :agent_reasoning,
                :ca_notes, :created_at, :checks, :recon_status, :bank_credit_amount,
                :bank_delta, :settlement_delay_days
            )
            """,
            row,
        )
        await self._db.commit()

    async def update_bank_credit(
        self,
        recon_id: str,
        bank_credit_amount: float,
        bank_delta: float,
        recon_status: str,
        checks: str,
    ) -> None:
        await self._db.execute(
            """
            UPDATE reconciliations
            SET bank_credit_amount = :bank_credit_amount,
                bank_delta = :bank_delta,
                recon_status = :recon_status,
                checks = :checks
            WHERE id = :id
            """,
            {
                "bank_credit_amount": bank_credit_amount,
                "bank_delta": bank_delta,
                "recon_status": recon_status,
                "checks": checks,
                "id": recon_id,
            },
        )
        await self._db.commit()

    async def get_reconciliations_by_run(self, run_id: str) -> list[dict]:
        return await self._fetchall(
            "SELECT * FROM reconciliations WHERE run_id = ? ORDER BY created_at",
            (run_id,),
        )

    # ------------------------------------------------------------------ NL->SQL
    async def execute_query(self, sql: str, params: tuple = ()) -> list[dict]:
        """Execute a read-only SELECT query (for NL->SQL feature)."""
        stripped = sql.strip().upper()
        if not stripped.startswith("SELECT"):
            raise ValueError("Only SELECT queries are permitted via execute_query()")
        return await self._fetchall(sql, params)
