"""
scorer.py — Vendor priority scoring engine for PRIYA personas.

Supports hospital and kirana persona scoring rules.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any


def _parse_date(d: str | date) -> date:
    if isinstance(d, date):
        return d
    return datetime.strptime(d, "%Y-%m-%d").date()


def score_vendors(
    vendors: list[dict],
    persona_config: dict,
    today: str,
) -> list[dict]:
    """Score and sort vendors by priority for a given persona.

    Args:
        vendors: List of vendor dicts (matches CSV columns).
        persona_config: Parsed YAML persona config dict.
        today: ISO date string (YYYY-MM-DD).

    Returns:
        Vendors sorted by priority_score descending, each enriched with:
          - priority_score (int)
          - priority_reason (str)
          - action: "pay" | "defer" | "escalate" | "queue"
    """
    today_date = _parse_date(today)
    persona = persona_config.get("persona", "")
    priority_rules = persona_config.get("priority_rules", {})
    category_scores: dict[str, int] = priority_rules.get("category_scores", {})
    vendor_type_scores: dict[str, int] = priority_rules.get("vendor_type_scores", {})
    overdue_bonus_per_day: int = priority_rules.get("overdue_bonus_per_day", 0)

    scored: list[dict] = []

    for vendor in vendors:
        vendor = dict(vendor)  # copy to avoid mutating input
        score = 0
        reasons: list[str] = []
        action = "pay"

        category = vendor.get("category", "")
        vendor_type = vendor.get("vendor_type", "")
        drug_schedule = vendor.get("drug_schedule", "") or ""
        credit_days = int(vendor.get("credit_days", 0) or 0)
        due_date_str = vendor.get("due_date", "") or ""

        # --- Category score ---
        cat_score = category_scores.get(category, 0)
        if cat_score:
            score += cat_score
            reasons.append(f"category:{category}+{cat_score}")

        # --- Vendor type score ---
        vt_score = vendor_type_scores.get(vendor_type, 0)
        if vt_score:
            score += vt_score
            reasons.append(f"vendor_type:{vendor_type}{'+' if vt_score >= 0 else ''}{vt_score}")

        if persona == "hospital":
            # --- Overdue bonus ---
            if due_date_str:
                due_date = _parse_date(due_date_str)
                overdue_days = (today_date - due_date).days
                if overdue_days > 0:
                    bonus = overdue_days * overdue_bonus_per_day
                    score += bonus
                    reasons.append(f"overdue:{overdue_days}d+{bonus}")

            # --- Compliance / drug schedule ---
            compliance_config = persona_config.get("compliance", {})
            if drug_schedule == "schedule_h":
                rule = compliance_config.get("schedule_h", {})
                action = rule.get("action", "escalate")
                reasons.append(f"drug_schedule:schedule_h->{action}")
            elif drug_schedule == "schedule_x":
                rule = compliance_config.get("schedule_x", {})
                action = rule.get("action", "queue")
                reasons.append(f"drug_schedule:schedule_x->{action}")

        elif persona == "kirana":
            credit_rules = priority_rules.get("credit_rules", {})
            defer_if_credit_remaining: bool = credit_rules.get("defer_if_credit_remaining", True)
            cod_bonus: int = credit_rules.get("cod_bonus", 0)
            expired_credit_bonus: int = credit_rules.get("expired_credit_bonus", 0)

            invoice_date_str = vendor.get("invoice_date", "") or ""

            if credit_days == 0:
                # COD vendor — pay immediately
                score += cod_bonus
                reasons.append(f"cod+{cod_bonus}")
            elif invoice_date_str:
                invoice_date = _parse_date(invoice_date_str)
                credit_due = date(
                    invoice_date.year, invoice_date.month, invoice_date.day
                )
                from datetime import timedelta
                credit_due = invoice_date + timedelta(days=credit_days)
                days_remaining = (credit_due - today_date).days

                if days_remaining < 0:
                    # Credit period expired
                    score += expired_credit_bonus
                    reasons.append(f"credit_expired:{abs(days_remaining)}d+{expired_credit_bonus}")
                elif defer_if_credit_remaining and days_remaining > 0:
                    action = "defer"
                    reasons.append(f"credit_remaining:{days_remaining}d->defer")

                # Attach float info
                float_info = compute_credit_float(
                    vendor=vendor,
                    invoice_date=invoice_date_str,
                    today=today,
                    cost_of_capital=0.12,  # default 12% annual
                )
                vendor["float_saved"] = float_info["float_saved"]
                vendor["days_remaining"] = float_info["days_remaining"]
                vendor["should_defer"] = float_info["should_defer"]

        vendor["priority_score"] = score
        vendor["priority_reason"] = "; ".join(reasons) if reasons else "base"
        vendor["action"] = action
        scored.append(vendor)

    scored.sort(key=lambda v: v["priority_score"], reverse=True)
    return scored


def compute_credit_float(
    vendor: dict,
    invoice_date: str,
    today: str,
    cost_of_capital: float,
) -> dict:
    """Compute credit float savings for a vendor.

    Args:
        vendor: Vendor dict with at least 'credit_days' and 'amount'.
        invoice_date: ISO date string for when invoice was issued.
        today: ISO date string for current date.
        cost_of_capital: Annual cost of capital as a decimal (e.g. 0.12 for 12%).

    Returns:
        dict with:
          - days_remaining (int): Days left in credit period (negative = overdue)
          - float_saved (float): INR value of float savings from deferral
          - should_defer (bool): True if credit days remain and deferral is beneficial
    """
    from datetime import timedelta

    invoice_dt = _parse_date(invoice_date)
    today_dt = _parse_date(today)
    credit_days = int(vendor.get("credit_days", 0) or 0)
    amount = float(vendor.get("amount", 0) or 0)

    credit_due = invoice_dt + timedelta(days=credit_days)
    days_remaining = (credit_due - today_dt).days

    # Float saved = amount * cost_of_capital * (days_remaining / 365)
    # Only positive when days_remaining > 0
    if days_remaining > 0:
        float_saved = round(amount * cost_of_capital * (days_remaining / 365), 2)
        should_defer = True
    else:
        float_saved = 0.0
        should_defer = False

    return {
        "days_remaining": days_remaining,
        "float_saved": float_saved,
        "should_defer": should_defer,
    }
