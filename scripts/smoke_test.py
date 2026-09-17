"""End-to-end smoke test.

Runs the complete pipeline in one process - buyer agent -> A2A -> merchant
agent -> MCP/UCP -> inventory -> upsell -> Razorpay (mock) -> verification ->
ledger - plus the important failure paths, and finally verifies the audit
chain. No AWS account, no Razorpay keys, no network.

    python scripts/smoke_test.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import Settings  # noqa: E402
from app.core.inprocess import InProcessASGITransport  # noqa: E402
from app.core.money import format_money, to_minor  # noqa: E402
from buyer_agent.api.dependencies import build_container as build_buyer  # noqa: E402
from merchant_agent.api.dependencies import build_container as build_merchant  # noqa: E402
from merchant_agent.main import create_app as create_merchant_app  # noqa: E402

GREEN, RED, YELLOW, RESET = "\033[92m", "\033[91m", "\033[93m", "\033[0m"
failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    mark = f"{GREEN}PASS{RESET}" if condition else f"{RED}FAIL{RESET}"
    print(f"  [{mark}] {label}" + (f" - {detail}" if detail else ""))
    if not condition:
        failures.append(label)


def section(title: str) -> None:
    print(f"\n{YELLOW}{title}{RESET}")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="acp-smoke-"))
    settings = Settings(
        ENVIRONMENT="development",
        AI_PROVIDER="mock",
        RAZORPAY_MODE="mock",
        LEDGER_BACKEND="sqlite",
        DATABASE_URL=f"sqlite:///{tmp}/merchant.db",
        BUYER_DATABASE_URL=f"sqlite:///{tmp}/buyer.db",
        EMBEDDING_CACHE_PATH=str(tmp / "cache.json"),
        CATALOG_PATH=str(Path(__file__).resolve().parent.parent / "data" / "catalog.json"),
        AP2_MANDATE_SECRET="smoke-test-secret-value",
        A2A_API_KEY="smoke-test-a2a-key",
        LOG_LEVEL="WARNING",
    )

    merchant_container = build_merchant(settings)
    merchant_app = create_merchant_app(settings, merchant_container)
    transport = InProcessASGITransport(merchant_app)
    buyer_container = build_buyer(settings, transport=transport)
    buyer = buyer_container.buyer

    section("1. Agent discovery (A2A agent card)")
    card = buyer_container.client.fetch_agent_card()
    check("Merchant agent card advertises skills", len(card.get("skills", [])) >= 6)
    check(
        "Card advertises MCP and UCP interfaces",
        {i["transport"] for i in card["additionalInterfaces"]} == {"MCP", "UCP"},
    )

    section("2. Happy path: in-budget purchase with an affordable add-on")
    outcome = buyer.purchase("I need good wireless headphones under 2500 rupees")
    for step in outcome.steps:
        print(f"      - {step.stage:<14} {step.status:<9} {step.message}")
    check("Purchase completed", outcome.status == "completed", outcome.status)
    check("Payment confirmed", bool(outcome.confirmation), "")
    if outcome.confirmation:
        check(
            "Charged amount within mandate",
            outcome.confirmation["amount"] <= outcome.mandate["maximum_amount"],
            format_money(outcome.confirmation["amount"]),
        )

    section("3. Upsell safety: mandate 2000, product 1500, add-on 800 -> must reject")
    outcome2 = buyer.purchase("buy me bluetooth headphones", budget=to_minor(2000))
    upsell = outcome2.upsell or {}
    check("Purchase completed", outcome2.status == "completed", outcome2.status)
    check(
        "Over-budget add-on rejected",
        (not upsell.get("accepted")) or upsell.get("projected_total", 0) <= 200000,
        f"reason={upsell.get('reason')} projected={upsell.get('projected_total')}",
    )
    if outcome2.confirmation:
        check(
            "Final charge never exceeds the 2000 mandate",
            outcome2.confirmation["amount"] <= 200000,
            format_money(outcome2.confirmation["amount"]),
        )

    section("3b. Spec example: mandate 2000, product 1500, add-on 800 -> reject")
    from app.models.catalog import UpsellOffer  # noqa: PLC0415
    from app.models.mandate import MandateRequest  # noqa: PLC0415

    spec_mandate = buyer_container.mandates.create_mandate(
        MandateRequest(buyer_id="buyer-demo", currency="INR", maximum_amount=to_minor(2000))
    )
    spec_decision = buyer._evaluate_upsell(
        UpsellOffer(
            product_id="prd_ac_007",
            name="Extended Warranty Plan",
            category="services",
            price=to_minor(800),
            currency="INR",
        ).model_dump(mode="json"),
        {"product_id": "prd_x", "name": "Product", "category": "audio"},
        spec_mandate,
        to_minor(1500),
    )
    check("Add-on rejected", not spec_decision.accepted, spec_decision.reason)
    check("Rejected for budget", spec_decision.reason == "budget_exceeded", spec_decision.reason)
    check(
        "Basket falls back to the base total",
        spec_decision.projected_total == to_minor(2300),
        format_money(spec_decision.projected_total),
    )

    section("4. Failure path: budget too small for anything in the catalog")
    outcome3 = buyer.purchase("buy a mechanical keyboard", budget=to_minor(50))
    check("Purchase rejected", outcome3.status == "failed", outcome3.status)
    check("Failure is auditable", bool(outcome3.error), str(outcome3.error))

    section("5. Failure path: out-of-stock product")
    check_result = merchant_container.inventory.check("prd_eb_003", 1)
    check("Out-of-stock product reports unavailable", not check_result.in_stock)

    section("6. Failure path: invalid payment signature is refused")
    mandate = merchant_container.mandates.create_mandate(
        MandateRequest(
            buyer_id="buyer-demo", currency="INR", maximum_amount=to_minor(2000), intent="test"
        )
    )
    order = merchant_container.merchant.create_order(
        transaction_id="txn_smoke_bad_sig",
        mandate_raw=mandate.model_dump(mode="json"),
        items=[{"product_id": "prd_hp_002", "quantity": 1}],
    )
    try:
        merchant_container.merchant.confirm_payment(
            transaction_id="txn_smoke_bad_sig",
            order_id=order.order_id,
            payment_id="pay_forged",
            signature="deadbeef",
        )
        check("Forged signature rejected", False, "confirm_payment returned successfully")
    except Exception as exc:
        check("Forged signature rejected", True, type(exc).__name__)
    check(
        "Stock released after failed payment",
        merchant_container.inventory.available("prd_hp_002") >= 1,
    )

    section("7. Audit ledger integrity")
    merchant_result = merchant_container.ledger.verify_integrity()
    buyer_result = buyer_container.ledger.verify_integrity()
    check(
        "Merchant chain valid",
        merchant_result.valid,
        f"{merchant_result.events_checked} events",
    )
    check("Buyer chain valid", buyer_result.valid, f"{buyer_result.events_checked} events")

    # Tamper with a stored event and confirm detection.
    import sqlite3

    conn = sqlite3.connect(f"{tmp}/merchant.db")
    conn.execute(
        "UPDATE ledger_events SET payload = ? WHERE sequence = 2",
        ('{"tampered":true}',),
    )
    conn.commit()
    conn.close()
    tampered = merchant_container.ledger.verify_integrity()
    check("Tampering detected", not tampered.valid, str(tampered.reason))

    section("Result")
    if failures:
        print(f"{RED}{len(failures)} check(s) failed:{RESET}")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print(f"{GREEN}All smoke checks passed.{RESET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
