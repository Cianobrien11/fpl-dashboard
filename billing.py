"""
billing.py — Stripe subscriptions for FPL IQ (single Pro tier).

Everything here is gated behind the BILLING_ENABLED master switch:

  BILLING_ENABLED=0 (default): billing is OFF. is_pro() returns True for
      EVERYONE, so all features stay unlocked during testing. No Stripe calls.
  BILLING_ENABLED=1: the paywall is live. is_pro() reflects the user's real
      plan; checkout + webhooks + the billing portal are active.

Env vars (only needed once BILLING_ENABLED=1):
  STRIPE_SECRET_KEY        sk_test_... (test mode) or sk_live_...
  STRIPE_PRICE_ID          the Price ID of your Pro plan (price_...)
  STRIPE_WEBHOOK_SECRET    whsec_... (from the Stripe webhook dashboard)

Build-and-test note: with BILLING_ENABLED=0 you can build/ship the whole
flow with ZERO Stripe setup — nothing charges, nothing locks.
"""
from __future__ import annotations

import os

import models

# Features that require Pro once billing is enabled. Used by the app's
# @pro_required gate and the UI to show lock badges.
PRO_FEATURES = {"planner", "ask", "analytics", "transfers"}


# TESTING MODE: subscriptions are hidden completely and every user gets full
# access, whatever BILLING_ENABLED says on Render. At launch, set this to True
# (then BILLING_ENABLED=1 on Render turns the Pro locks + Stripe back on).
BILLING_LIVE = False


def billing_enabled() -> bool:
    if not BILLING_LIVE:
        return False
    return os.environ.get("BILLING_ENABLED", "0") == "1"


def pro_price() -> str:
    return os.environ.get("PRO_PRICE_LABEL", "\u20ac4.99/mo")


def is_pro(user_id: int) -> bool:
    """Is this user entitled to Pro features?

    When billing is OFF, everyone is Pro (full access for testing). When ON,
    only users whose plan is 'pro' with an active/trialing status qualify."""
    if not billing_enabled():
        return True
    if not user_id:
        return False
    p = models.get_user_plan(user_id)
    if p.get("plan") != "pro":
        return False
    status = (p.get("sub_status") or "").lower()
    return status in ("active", "trialing", "")  # '' = set directly, treat as active


def _stripe():
    """Return the configured stripe module, or None if not set up."""
    key = os.environ.get("STRIPE_SECRET_KEY", "").strip()
    if not key:
        return None
    try:
        import stripe
        stripe.api_key = key
        return stripe
    except Exception:
        return None


def create_checkout_session(user_id: int, email: str, success_url: str,
                            cancel_url: str) -> dict:
    """Create a Stripe Checkout session for the Pro subscription.
    Returns {ok, url, error}."""
    stripe = _stripe()
    price_id = os.environ.get("STRIPE_PRICE_ID", "").strip()
    if not stripe or not price_id:
        return {"ok": False, "error": "Billing is not configured yet."}
    try:
        existing = models.get_user_plan(user_id).get("stripe_customer_id")
        kwargs = {
            "mode": "subscription",
            "line_items": [{"price": price_id, "quantity": 1}],
            "success_url": success_url,
            "cancel_url": cancel_url,
            "client_reference_id": str(user_id),
            "metadata": {"user_id": str(user_id)},
        }
        if existing:
            kwargs["customer"] = existing
        else:
            kwargs["customer_email"] = email
        sess = stripe.checkout.Session.create(**kwargs)
        return {"ok": True, "url": sess.url, "error": None}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def create_portal_session(user_id: int, return_url: str) -> dict:
    """Billing portal so users manage/cancel their subscription themselves."""
    stripe = _stripe()
    if not stripe:
        return {"ok": False, "error": "Billing is not configured yet."}
    cust = models.get_user_plan(user_id).get("stripe_customer_id")
    if not cust:
        return {"ok": False, "error": "No subscription on file."}
    try:
        sess = stripe.billing_portal.Session.create(customer=cust, return_url=return_url)
        return {"ok": True, "url": sess.url, "error": None}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def handle_webhook(payload: bytes, sig_header: str) -> dict:
    """Verify + process a Stripe webhook event. Returns {ok, error}.

    Handles the subscription lifecycle: checkout completed, subscription
    updated/deleted → flips the user's plan in the DB."""
    stripe = _stripe()
    secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip()
    if not stripe or not secret:
        return {"ok": False, "error": "Webhook not configured."}
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, secret)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"Signature verification failed: {exc}"}

    etype = event.get("type", "")
    obj = event.get("data", {}).get("object", {})

    try:
        if etype == "checkout.session.completed":
            uid = int(obj.get("metadata", {}).get("user_id")
                      or obj.get("client_reference_id") or 0)
            customer = obj.get("customer")
            if uid and customer:
                models.set_stripe_customer(uid, customer)
                models.set_user_plan(uid, "pro", "active", None)
        elif etype in ("customer.subscription.updated", "customer.subscription.created"):
            customer = obj.get("customer")
            status = obj.get("status")
            until = None
            try:
                import datetime as _dt
                ts = obj.get("current_period_end")
                if ts:
                    until = _dt.datetime.fromtimestamp(ts, _dt.timezone.utc).isoformat()
            except Exception:
                pass
            u = models.get_user_by_stripe_customer(customer)
            if u:
                plan = "pro" if status in ("active", "trialing") else "free"
                models.set_user_plan(u["id"], plan, status, until)
        elif etype == "customer.subscription.deleted":
            customer = obj.get("customer")
            u = models.get_user_by_stripe_customer(customer)
            if u:
                models.set_user_plan(u["id"], "free", "canceled", None)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "error": None}
