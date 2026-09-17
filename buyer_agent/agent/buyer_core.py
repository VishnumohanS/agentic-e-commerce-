"""Buyer agent reasoning and orchestration.

Division of responsibility - this is the heart of the safety story:

* The **model** (Bedrock) does language work only: reading the request, ranking
  candidates, and giving a non-binding opinion on an upsell.
* **Deterministic Python** does everything that decides whether money moves:
  minting the bounded AP2 mandate, filtering candidates by price, and the
  `base + upsell <= maximum` arithmetic.

If the model returned garbage, hallucinated a price or was prompt-injected by
the merchant's product copy, the worst outcome is a bad *recommendation*, never
an over-budget or out-of-scope purchase.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Callable, Iterator

from app.core.exceptions import (
    AgenticCommerceError,
    AIProviderError,
    BudgetExceededError,
    PurchaseRejectedError,
)
from app.core.logging import get_logger
from app.core.money import format_money, to_minor
from app.models.catalog import UpsellDecision, UpsellOffer
from app.models.ledger import EventType
from app.models.mandate import LineItem, MandateRequest, SpendingMandate
from app.services.ai_provider import AIProvider
from app.services.ap2 import MandateService
from app.services.ledger import AuditLedger
from buyer_agent.services.a2a_client import A2AClient

logger = get_logger(__name__)

ACTOR = "buyer_agent"

INTENT_SYSTEM = (
    "You are the language-understanding component of an autonomous buyer agent. "
    "Extract structured shopping intent from the user's request. You do not make "
    "purchase decisions and you never decide budgets - you only report what the "
    "user asked for."
)

RANKING_SYSTEM = (
    "You rank candidate products by how well they match a shopping request. "
    "All candidates have already been filtered to be affordable and in stock. "
    "Return only an ordering; you cannot add products or change prices."
)

UPSELL_SYSTEM = (
    "You give a non-binding opinion on whether an add-on complements a product. "
    "Budget enforcement is handled elsewhere and your answer cannot override it."
)


@dataclass
class PurchaseStep:
    """One entry in the buyer's decision trail (also streamed to the UI)."""

    stage: str
    status: str
    message: str
    data: dict[str, Any] = field(default_factory=dict)
    at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "status": self.status,
            "message": self.message,
            "data": self.data,
            "at": self.at,
        }


@dataclass
class PurchaseOutcome:
    transaction_id: str
    status: str
    steps: list[PurchaseStep]
    mandate: dict[str, Any] | None = None
    product: dict[str, Any] | None = None
    upsell: dict[str, Any] | None = None
    order: dict[str, Any] | None = None
    confirmation: dict[str, Any] | None = None
    error: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "transaction_id": self.transaction_id,
            "status": self.status,
            "mandate": self.mandate,
            "product": self.product,
            "upsell": self.upsell,
            "order": self.order,
            "confirmation": self.confirmation,
            "error": self.error,
            "steps": [step.to_dict() for step in self.steps],
        }


class BuyerAgent:
    """Turns a natural-language request into a bounded, audited purchase."""

    def __init__(
        self,
        *,
        provider: AIProvider,
        mandates: MandateService,
        client: A2AClient,
        ledger: AuditLedger,
        buyer_id: str = "buyer-demo",
        default_budget: int = 200000,
    ) -> None:
        self._provider = provider
        self._mandates = mandates
        self._client = client
        self._ledger = ledger
        self._buyer_id = buyer_id
        self._default_budget = default_budget

    # --- public API ------------------------------------------------------

    def purchase(
        self,
        request_text: str,
        *,
        budget: int | None = None,
        auto_pay: bool = True,
        buyer_id: str | None = None,
    ) -> PurchaseOutcome:
        """Run the whole flow and return the outcome (non-streaming)."""
        steps: list[PurchaseStep] = []
        outcome: PurchaseOutcome | None = None
        for event in self.purchase_stream(
            request_text, budget=budget, auto_pay=auto_pay, buyer_id=buyer_id
        ):
            if isinstance(event, PurchaseStep):
                steps.append(event)
            else:
                outcome = event
        assert outcome is not None  # the stream always ends with an outcome
        return outcome

    def purchase_stream(
        self,
        request_text: str,
        *,
        budget: int | None = None,
        auto_pay: bool = True,
        buyer_id: str | None = None,
    ) -> Iterator[PurchaseStep | PurchaseOutcome]:
        """Yield each decision as it happens, then the final outcome."""
        transaction_id = f"txn_{uuid.uuid4().hex[:16]}"
        buyer_id = buyer_id or self._buyer_id
        steps: list[PurchaseStep] = []

        def emit(stage: str, status: str, message: str, **data: Any) -> PurchaseStep:
            step = PurchaseStep(stage=stage, status=status, message=message, data=data)
            steps.append(step)
            return step

        def audit(event_type: str, payload: dict[str, Any]) -> None:
            self._ledger.append(
                event_type, transaction_id=transaction_id, payload=payload, actor=ACTOR
            )

        request_text = (request_text or "").strip()
        if not request_text:
            error = PurchaseRejectedError("Purchase request cannot be empty")
            audit(EventType.PURCHASE_FAILED, {"stage": "input", **error.to_dict()})
            yield emit("input", "failed", error.message)
            yield PurchaseOutcome(
                transaction_id=transaction_id,
                status="rejected",
                steps=steps,
                error=error.to_dict(),
            )
            return

        audit(
            EventType.PURCHASE_REQUESTED,
            {"request": request_text[:500], "buyer_id": buyer_id, "budget_hint": budget},
        )
        yield emit("request", "ok", "Purchase request received", request=request_text[:500])

        mandate: SpendingMandate | None = None
        try:
            # 1. Interpret intent (model, advisory only).
            intent = self._interpret_intent(request_text)
            audit(EventType.INTENT_INTERPRETED, intent)
            yield emit(
                "intent",
                "ok",
                intent.get("summary") or "Interpreted the request",
                query=intent.get("query"),
                category=intent.get("category"),
                quantity=intent.get("quantity"),
            )

            # 2. Mint the bounded AP2 mandate (deterministic).
            mandate = self._create_mandate(request_text, intent, budget, buyer_id)
            audit(
                EventType.MANDATE_CREATED,
                {
                    "mandate_id": mandate.mandate_id,
                    "maximum_amount": mandate.maximum_amount,
                    "currency": mandate.currency,
                    "allowed_categories": mandate.allowed_categories,
                    "expires_at": mandate.expires_at.isoformat(),
                },
            )
            yield emit(
                "mandate",
                "ok",
                f"Spending mandate created with a hard ceiling of "
                f"{format_money(mandate.maximum_amount, mandate.currency)}",
                mandate=mandate.public_view(),
            )

            # 3. Search the merchant catalog over A2A/MCP.
            quantity = max(1, int(intent.get("quantity") or 1))
            max_unit_price = mandate.maximum_amount // quantity
            search = self._client.search_catalog(
                intent.get("query") or request_text,
                transaction_id=transaction_id,
                max_price=max_unit_price,
                category=intent.get("category") or None,
                limit=5,
            )
            results = search.get("results", [])
            yield emit(
                "search",
                "ok",
                f"Found {len(results)} affordable in-stock option(s)",
                count=len(results),
                candidates=[
                    {"product_id": r["product_id"], "name": r["name"], "price": r["price"]}
                    for r in results
                ],
            )
            if not results:
                raise PurchaseRejectedError(
                    "No in-stock product matches the request within the mandate budget",
                    details={"budget": mandate.maximum_amount, "quantity": quantity},
                )

            # 4. Select a product (model ranks; Python re-checks affordability).
            product = self._select_product(intent, results, mandate, quantity)
            audit(
                EventType.PRODUCT_SELECTED,
                {
                    "product_id": product["product_id"],
                    "price": product["price"],
                    "quantity": quantity,
                },
            )
            yield emit(
                "selection",
                "ok",
                f"Selected {product['name']} at {format_money(product['price'])}",
                product=product,
                quantity=quantity,
            )

            # 5. Quote, and evaluate any upsell against the mandate.
            quote = self._client.quote(
                transaction_id=transaction_id,
                product_id=product["product_id"],
                quantity=quantity,
                mandate=mandate.model_dump(mode="json"),
            )
            base_total = int(quote["base_total"])
            decision = self._evaluate_upsell(quote.get("upsell_offer"), product, mandate, base_total)
            audit(
                EventType.UPSELL_ACCEPTED if decision.accepted else EventType.UPSELL_EVALUATED,
                decision.model_dump(mode="json"),
            )
            if decision.offer is not None:
                yield emit(
                    "upsell",
                    "accepted" if decision.accepted else "rejected",
                    self._upsell_message(decision),
                    decision=decision.model_dump(mode="json"),
                )
            else:
                yield emit("upsell", "skipped", "Merchant proposed no add-on")

            # 6. Build the basket and re-check locally before sending anything.
            items = [
                LineItem(
                    product_id=product["product_id"],
                    name=product["name"],
                    category=product.get("category", ""),
                    unit_price=int(product["price"]),
                    quantity=quantity,
                    kind="primary",
                )
            ]
            if decision.accepted and decision.offer is not None:
                items.append(
                    LineItem(
                        product_id=decision.offer.product_id,
                        name=decision.offer.name,
                        category=decision.offer.category,
                        unit_price=decision.offer.price,
                        quantity=decision.offer.quantity,
                        kind="upsell",
                    )
                )
            authorization = self._mandates.authorize(mandate, items, currency=mandate.currency)
            if not authorization.approved:
                raise BudgetExceededError(
                    "Basket failed the buyer-side mandate check",
                    details={"violations": authorization.violations},
                )
            yield emit(
                "authorization",
                "ok",
                f"Basket total {format_money(authorization.total_amount)} is within the mandate "
                f"({format_money(mandate.maximum_amount)})",
                total=authorization.total_amount,
                remaining=authorization.remaining_budget,
            )

            # 7. Ask the merchant to create the order.
            order = self._client.create_order(
                transaction_id=transaction_id,
                mandate=mandate.model_dump(mode="json"),
                items=[item.model_dump() for item in items],
                buyer_id=buyer_id,
                currency=mandate.currency,
            )
            yield emit(
                "order",
                "ok",
                f"Razorpay test order {order['order_id']} created for "
                f"{format_money(int(order['amount']))}",
                order=order,
            )

            if not auto_pay:
                yield emit(
                    "payment",
                    "pending",
                    "Awaiting checkout - complete the payment to confirm this order",
                )
                yield PurchaseOutcome(
                    transaction_id=transaction_id,
                    status="awaiting_payment",
                    steps=steps,
                    mandate=mandate.public_view(),
                    product=product,
                    upsell=decision.model_dump(mode="json"),
                    order=order,
                )
                return

            # 8. Complete checkout, then confirm (merchant verifies independently).
            payment = self._client.simulate_payment(order["order_id"])
            yield emit(
                "payment",
                "ok",
                "Checkout completed; awaiting merchant verification",
                payment_id=payment["payment_id"],
            )

            confirmation = self._client.confirm(
                transaction_id=transaction_id,
                order_id=order["order_id"],
                payment_id=payment["payment_id"],
                signature=payment["signature"],
            )
            audit(
                EventType.PURCHASE_COMPLETED,
                {
                    "order_id": confirmation["order_id"],
                    "payment_id": confirmation["payment_id"],
                    "amount": confirmation["amount"],
                    "mandate_id": mandate.mandate_id,
                },
            )
            yield emit(
                "confirmation",
                "ok",
                f"Purchase confirmed for {format_money(int(confirmation['amount']))}",
                confirmation=confirmation,
            )
            yield PurchaseOutcome(
                transaction_id=transaction_id,
                status="completed",
                steps=steps,
                mandate=mandate.public_view(),
                product=product,
                upsell=decision.model_dump(mode="json"),
                order=order,
                confirmation=confirmation,
            )
            return

        except AgenticCommerceError as exc:
            audit(EventType.PURCHASE_FAILED, {"stage": "purchase", **exc.to_dict()})
            logger.warning(
                "purchase.failed",
                extra={
                    "transaction_id": transaction_id,
                    "error_code": exc.code,
                    "error_type": type(exc).__name__,
                },
            )
            yield emit("failure", "failed", exc.message, code=exc.code, details=exc.details)
            yield PurchaseOutcome(
                transaction_id=transaction_id,
                status="failed",
                steps=steps,
                mandate=mandate.public_view() if mandate else None,
                error=exc.to_dict(),
            )
            return
        except Exception as exc:  # unexpected: still auditable, never silent
            audit(
                EventType.PURCHASE_FAILED,
                {"stage": "purchase", "code": "internal_error", "message": type(exc).__name__},
            )
            logger.exception(
                "purchase.unexpected_error", extra={"transaction_id": transaction_id}
            )
            yield emit("failure", "failed", "An unexpected error occurred")
            yield PurchaseOutcome(
                transaction_id=transaction_id,
                status="failed",
                steps=steps,
                error={"code": "internal_error", "message": "An unexpected error occurred"},
            )
            return

    # --- reasoning helpers ----------------------------------------------

    def _ai_json(self, task: str, system: str, payload: dict[str, Any]) -> dict[str, Any]:
        prompt = (
            f"TASK: {task}\n"
            f"INPUT_JSON: {json.dumps(payload, ensure_ascii=False)}\n"
        )
        return self._provider.complete_json(prompt, system=system)

    def _safe_ai_json(
        self, task: str, system: str, payload: dict[str, Any], fallback: Callable[[], dict[str, Any]]
    ) -> dict[str, Any]:
        """Model failures degrade to a deterministic fallback, never a crash."""
        try:
            result = self._ai_json(task, system, payload)
            self._ledger_ai_event(task, "ok")
            return result
        except (AIProviderError, Exception) as exc:  # noqa: BLE001 - deliberate catch-all
            logger.warning(
                "ai.task_failed", extra={"task": task, "error_type": type(exc).__name__}
            )
            self._ledger_ai_event(task, "failed", error_type=type(exc).__name__)
            return fallback()

    def _ledger_ai_event(self, task: str, status: str, **extra: Any) -> None:
        try:
            self._ledger.append(
                EventType.AI_INVOKED if status == "ok" else EventType.AI_FAILED,
                transaction_id=str(uuid.uuid4())[:8],
                payload={
                    "task": task,
                    "status": status,
                    "provider": self._provider.name,
                    "model_id": self._provider.model_id,
                    **extra,
                },
                actor=ACTOR,
            )
        except Exception:  # pragma: no cover - auditing must not break the flow
            logger.warning("ai.audit_failed", extra={"task": task})

    def _interpret_intent(self, request_text: str) -> dict[str, Any]:
        raw = self._safe_ai_json(
            "intent_extraction",
            INTENT_SYSTEM,
            {
                "request": request_text,
                "schema": {
                    "query": "string",
                    "category": "string or empty",
                    "budget_major": "number or null",
                    "quantity": "integer >= 1",
                    "must_have": ["string"],
                    "summary": "string",
                },
            },
            fallback=lambda: {"query": request_text, "quantity": 1, "category": ""},
        )
        quantity = raw.get("quantity")
        try:
            quantity = max(1, min(int(quantity), 10))
        except (TypeError, ValueError):
            quantity = 1
        budget_major = raw.get("budget_major")
        try:
            budget_minor = to_minor(budget_major) if budget_major not in (None, "") else None
        except ValueError:
            budget_minor = None
        return {
            "query": str(raw.get("query") or request_text)[:300],
            "category": str(raw.get("category") or "")[:50],
            "budget_minor": budget_minor,
            "quantity": quantity,
            "must_have": [str(k)[:30] for k in (raw.get("must_have") or [])][:6],
            "summary": str(raw.get("summary") or "")[:300],
        }

    def _create_mandate(
        self,
        request_text: str,
        intent: dict[str, Any],
        budget: int | None,
        buyer_id: str,
    ) -> SpendingMandate:
        """The explicit user budget always wins over anything the model inferred."""
        maximum = budget if budget and budget > 0 else intent.get("budget_minor")
        if not maximum or maximum <= 0:
            maximum = self._default_budget
        # Scope covers the requested category plus add-on categories, otherwise
        # every upsell would be rejected as out-of-scope before the budget gate
        # ever ran. The spending ceiling is unchanged either way.
        categories: list[str] = []
        if intent.get("category"):
            categories = [intent["category"], "accessories", "services"]
        return self._mandates.create_mandate(
            MandateRequest(
                buyer_id=buyer_id,
                currency="INR",
                maximum_amount=int(maximum),
                allowed_categories=categories,
                max_items=max(2, int(intent.get("quantity", 1)) + 1),
                allow_upsell=True,
                intent=request_text,
            )
        )

    def _select_product(
        self,
        intent: dict[str, Any],
        results: list[dict[str, Any]],
        mandate: SpendingMandate,
        quantity: int,
    ) -> dict[str, Any]:
        """Model ranks; Python enforces affordability and stock independently."""
        affordable = [
            r
            for r in results
            if int(r.get("price", 0)) * quantity <= mandate.maximum_amount
            and int(r.get("available_quantity", r.get("inventory", 0))) >= quantity
        ]
        if not affordable:
            raise PurchaseRejectedError(
                "No candidate is both affordable within the mandate and in stock",
                details={"maximum_amount": mandate.maximum_amount, "quantity": quantity},
            )

        ranking = self._safe_ai_json(
            "product_ranking",
            RANKING_SYSTEM,
            {
                "query": intent.get("query", ""),
                "must_have": intent.get("must_have", []),
                "candidates": [
                    {
                        "product_id": r["product_id"],
                        "name": r["name"],
                        "category": r.get("category", ""),
                        "brand": r.get("brand", ""),
                        "description": r.get("description", "")[:300],
                        "price": r["price"],
                    }
                    for r in affordable
                ],
            },
            fallback=lambda: {"ranked_product_ids": [r["product_id"] for r in affordable]},
        )
        by_id = {r["product_id"]: r for r in affordable}
        for product_id in ranking.get("ranked_product_ids") or []:
            if product_id in by_id:  # a hallucinated id simply cannot be selected
                return by_id[product_id]
        return affordable[0]

    def _evaluate_upsell(
        self,
        offer_raw: dict[str, Any] | None,
        product: dict[str, Any],
        mandate: SpendingMandate,
        base_total: int,
    ) -> UpsellDecision:
        """Deterministic upsell safety: accept only if the *total* fits the mandate.

        Example from the spec: mandate 2000, product 1500, upsell 800 -> total
        2300 > 2000, so the upsell is rejected even if the model loves it.
        """
        if not offer_raw:
            return UpsellDecision(
                accepted=False,
                reason="no_offer",
                base_total=base_total,
                projected_total=base_total,
                mandate_maximum=mandate.maximum_amount,
            )
        try:
            offer = UpsellOffer.model_validate(offer_raw)
        except Exception:
            return UpsellDecision(
                accepted=False,
                reason="malformed_offer",
                base_total=base_total,
                projected_total=base_total,
                mandate_maximum=mandate.maximum_amount,
            )

        projected_total = base_total + offer.total

        if not mandate.allow_upsell:
            return UpsellDecision(
                offer=offer,
                accepted=False,
                reason="upsell_not_permitted",
                base_total=base_total,
                projected_total=projected_total,
                mandate_maximum=mandate.maximum_amount,
            )
        if offer.currency.upper() != mandate.currency.upper():
            return UpsellDecision(
                offer=offer,
                accepted=False,
                reason="currency_mismatch",
                base_total=base_total,
                projected_total=projected_total,
                mandate_maximum=mandate.maximum_amount,
            )
        if (
            mandate.allowed_categories
            and offer.category.lower() not in mandate.allowed_categories
        ):
            return UpsellDecision(
                offer=offer,
                accepted=False,
                reason="out_of_scope",
                base_total=base_total,
                projected_total=projected_total,
                mandate_maximum=mandate.maximum_amount,
            )
        # The hard gate. Integer comparison, no model involvement.
        if projected_total > mandate.maximum_amount:
            return UpsellDecision(
                offer=offer,
                accepted=False,
                reason="budget_exceeded",
                base_total=base_total,
                projected_total=projected_total,
                mandate_maximum=mandate.maximum_amount,
            )

        advisory = self._safe_ai_json(
            "upsell_advisory",
            UPSELL_SYSTEM,
            {
                "offer": offer.model_dump(mode="json"),
                "base_product": {
                    "product_id": product.get("product_id"),
                    "name": product.get("name"),
                    "category": product.get("category", ""),
                },
                "remaining_budget": mandate.maximum_amount - projected_total,
            },
            fallback=lambda: {"desirable": False, "rationale": "advisory_unavailable"},
        )
        desirable = bool(advisory.get("desirable"))
        return UpsellDecision(
            offer=offer,
            accepted=desirable,
            reason="accepted" if desirable else "not_desirable",
            base_total=base_total,
            projected_total=projected_total if desirable else base_total,
            mandate_maximum=mandate.maximum_amount,
            advisory=str(advisory.get("rationale", ""))[:200],
        )

    @staticmethod
    def _upsell_message(decision: UpsellDecision) -> str:
        offer = decision.offer
        assert offer is not None
        if decision.accepted:
            return (
                f"Accepted add-on {offer.name} ({format_money(offer.total)}); "
                f"total {format_money(decision.projected_total)} stays within the mandate"
            )
        if decision.reason == "budget_exceeded":
            return (
                f"Rejected add-on {offer.name}: total would be "
                f"{format_money(decision.projected_total)}, over the "
                f"{format_money(decision.mandate_maximum)} mandate limit"
            )
        return f"Rejected add-on {offer.name} ({decision.reason})"

    # --- lookups ---------------------------------------------------------

    def transaction_trail(self, transaction_id: str) -> list[dict[str, Any]]:
        return [event.model_dump() for event in self._ledger.read_transaction(transaction_id)]
