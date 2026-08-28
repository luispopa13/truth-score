"""
TruthScore -- Stripe Payments
"""
from config import *
from models import *


async def create_checkout(req: CheckoutRequest, user=Depends(require_user)):
    """Create Stripe checkout session."""
    # Always read fresh from env
    _sk = os.getenv("STRIPE_SECRET_KEY", "")
    if not _sk:
        raise HTTPException(503, "Stripe not configured -- adaugă STRIPE_SECRET_KEY în .env")
    stripe.api_key = _sk

    # Reload PLANS with fresh env values
    from auth import _get_plans
    _plans = _get_plans()
    plan = _plans.get(req.plan)
    if not plan:
        raise HTTPException(400, f"Plan necunoscut: {req.plan}")
    price_id = plan.get("price_id", "")
    if not price_id:
        raise HTTPException(400, f"Price ID lipsă pentru planul {req.plan}. Adaugă STRIPE_PRO_PRICE_ID sau STRIPE_ENT_PRICE_ID în .env")
    print(f"[STRIPE] Creating checkout: plan={req.plan}, price_id={price_id}, email={user.get('email')}")
    # Append the Stripe session-id placeholder with the correct separator:
    # "?" when success_url has no query string yet, "&" when it already does.
    _sep = "&" if "?" in req.success_url else "?"
    success_url = req.success_url + _sep + "session_id={CHECKOUT_SESSION_ID}"
    try:
        session = stripe.checkout.Session.create(
            mode="subscription",
            payment_method_types=["card"],
            line_items=[{"price": price_id, "quantity": 1}],
            success_url=success_url,
            cancel_url=req.cancel_url,
            customer_email=user["email"],
            metadata={"user_id": user["id"], "plan": req.plan},
        )
        print(f"[STRIPE] Session created: {session.url[:50]}")
        return {"checkout_url": session.url}
    except stripe.error.StripeError as e:
        print(f"[STRIPE ERROR] {e}")
        raise HTTPException(400, f"Stripe error: {str(e)}")
    except Exception as e:
        print(f"[STRIPE EXCEPTION] {type(e).__name__}: {e}")
        raise HTTPException(500, str(e))


async def _already_processed(event_id: str) -> bool:
    """Idempotency guard. Stripe delivers each event AT LEAST once (retries on
    non-2xx, network blips, dashboard resends), so a naive handler can upgrade a
    user twice or downgrade someone who already re-subscribed. We record every
    processed event id in a `stripe_events` collection and skip repeats.

    Uses the event id as the Mongo _id so the insert itself is the atomic
    dedup: a duplicate raises DuplicateKeyError -> we return True (skip). If no
    DB is configured we can't dedup, so we return False (process best-effort) —
    correctness of billing beats a theoretical double-process in a DB-less dev run.
    """
    if not (event_id and AUTH_AVAILABLE):
        return False
    try:
        from auth import get_db
        from datetime import datetime, timezone
        db = get_db()
        await db.stripe_events.insert_one(
            {"_id": event_id, "ts": datetime.now(timezone.utc)})
        return False   # inserted cleanly -> first time we've seen it
    except Exception as e:
        # DuplicateKeyError -> already handled. Any other error -> don't block
        # the event (log + process); a missed downgrade is worse than a rare retry.
        if "duplicate" in str(e).lower() or "E11000" in str(e):
            print(f"[STRIPE] Duplicate event {event_id} — skipping (idempotent).")
            return True
        print(f"[STRIPE] Idempotency check failed ({e}) — processing anyway.")
        return False


def _plan_for_price_id(price_id: str) -> str:
    """Map a Stripe price id back to our internal plan name. Empty/unknown -> ''."""
    if not price_id:
        return ""
    from auth import _get_plans
    for name, plan in _get_plans().items():
        if plan.get("price_id") and plan["price_id"] == price_id:
            return name
    return ""


async def _forget_event(event_id: str) -> None:
    """Undo the idempotency marker so a failed event can be retried by Stripe.

    We record the event id BEFORE doing the work (so concurrent redeliveries
    can't both process it). If the work then fails, the marker would otherwise
    make Stripe's retry look like a duplicate and silently drop it — a paid user
    never upgraded. Deleting the marker on failure lets the retry re-run; the
    underlying $set updates are idempotent, so reprocessing is safe.
    """
    if not (event_id and AUTH_AVAILABLE):
        return
    try:
        from auth import get_db
        await get_db().stripe_events.delete_one({"_id": event_id})
    except Exception as e:
        print(f"[STRIPE] Could not roll back event marker {event_id}: {e}")


async def _set_plan_by_customer(cust_id: str, updates: dict) -> None:
    """Apply a $set to the user matched by Stripe customer id (idempotent)."""
    if not (cust_id and AUTH_AVAILABLE):
        return
    from auth import get_db
    db = get_db()
    await db.users.update_one({"stripe_customer_id": cust_id}, {"$set": updates})


async def stripe_webhook(request: Request):
    """Handle Stripe webhooks — full subscription lifecycle, idempotent.

    Verifies the signature, dedups by event id, then routes:
      • checkout.session.completed      -> upgrade to purchased plan
      • customer.subscription.updated   -> sync plan from the active price;
                                           flag cancel_at_period_end
      • customer.subscription.deleted   -> downgrade to free
      • customer.subscription.paused    -> downgrade to free
      • invoice.payment_failed          -> mark past_due (Stripe keeps retrying;
                                           we don't hard-downgrade yet)
      • invoice.paid / payment_succeeded-> clear past_due, keep plan active
    """
    if not STRIPE_AVAILABLE:
        return {"status": "stripe not configured"}
    payload = await request.body()
    sig     = request.headers.get("stripe-signature", "")
    try:
        event = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
    except Exception as e:
        raise HTTPException(400, str(e))

    event_id   = event.get("id", "")
    event_type = event.get("type", "")
    if await _already_processed(event_id):
        return {"status": "duplicate", "id": event_id}

    obj = event["data"]["object"]

    try:
        if event_type == "checkout.session.completed":
            user_id = obj.get("metadata", {}).get("user_id")
            plan    = obj.get("metadata", {}).get("plan", "pro")
            cust_id = obj.get("customer", "")
            sub_id  = obj.get("subscription", "")
            if user_id:
                await upgrade_user_plan(user_id, plan, cust_id, sub_id)
                print(f"[STRIPE] Upgraded user {user_id} -> {plan}")

        elif event_type == "customer.subscription.updated":
            cust_id = obj.get("customer", "")
            # Derive the plan from the subscription's active price. On downgrade,
            # upgrade, or plan switch inside the portal, this keeps us in sync.
            price_id = ""
            try:
                price_id = obj["items"]["data"][0]["price"]["id"]
            except Exception:
                pass
            status  = obj.get("status", "")          # active, past_due, canceled, ...
            cancel_at_end = bool(obj.get("cancel_at_period_end"))
            updates = {"subscription_status": status,
                       "cancel_at_period_end": cancel_at_end}
            plan = _plan_for_price_id(price_id)
            # Only move the plan for a genuinely active/trialing subscription; a
            # past_due/unpaid sub keeps its plan until deletion (Stripe retries).
            if plan and status in ("active", "trialing"):
                updates["plan"] = plan
            await _set_plan_by_customer(cust_id, updates)
            print(f"[STRIPE] Subscription updated cust={cust_id} status={status} "
                  f"plan={plan or '(unchanged)'} cancel_at_end={cancel_at_end}")

        elif event_type in ("customer.subscription.deleted", "customer.subscription.paused"):
            cust_id = obj.get("customer", "")
            await _set_plan_by_customer(cust_id, {
                "plan": "free",
                "subscription_status": "canceled",
                "cancel_at_period_end": False,
            })
            print(f"[STRIPE] Subscription ended cust={cust_id} -> downgraded to free")

        elif event_type == "invoice.payment_failed":
            cust_id = obj.get("customer", "")
            # Don't hard-downgrade — Stripe will retry per the dunning schedule.
            # Flag it so the UI can warn the user; deletion event handles final loss.
            await _set_plan_by_customer(cust_id, {"subscription_status": "past_due"})
            print(f"[STRIPE] Payment failed cust={cust_id} -> flagged past_due")

        elif event_type in ("invoice.paid", "invoice.payment_succeeded"):
            cust_id = obj.get("customer", "")
            await _set_plan_by_customer(cust_id, {"subscription_status": "active"})
            print(f"[STRIPE] Payment succeeded cust={cust_id} -> active")
    except Exception as e:
        # We already recorded event_id as processed; if the work failed, roll the
        # marker back so Stripe's redelivery re-runs it instead of being deduped.
        print(f"[STRIPE] Handler failed for {event_type} ({event_id}): {e} — rolling back marker for retry")
        await _forget_event(event_id)
        raise HTTPException(500, "webhook processing failed")

    return {"status": "ok", "handled": event_type}


async def customer_portal(user=Depends(require_user)):
    """Redirect user to Stripe billing portal."""
    if not STRIPE_AVAILABLE:
        raise HTTPException(503, "Stripe not configured")
    cust_id = user.get("stripe_customer_id")
    if not cust_id:
        raise HTTPException(400, "No subscription found")
    session = stripe.billing_portal.Session.create(
        customer=cust_id,
        return_url=f"{PUBLIC_BASE_URL}/app",
    )
    return {"portal_url": session.url}
