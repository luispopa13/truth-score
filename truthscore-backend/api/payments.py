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
    # Build the redirect URLs server-side from the trusted base URL — never from
    # client input (open-redirect protection). Fails closed in prod if unset.
    base = get_public_base_url()
    success_url = f"{base}/?payment=success&session_id={{CHECKOUT_SESSION_ID}}"
    cancel_url  = f"{base}/"

    # If the user already holds an active/trialing subscription, a second checkout
    # would create a DUPLICATE subscription (double-billing). Route them to the
    # billing portal to change plans instead of minting a new one.
    if user.get("subscription_status") in ("active", "trialing") and user.get("stripe_customer_id"):
        raise HTTPException(409, "Ai deja un abonament activ. Folosește portalul de facturare pentru a schimba planul.")

    # Reuse the existing Stripe customer if we have one so a returning subscriber
    # doesn't spawn a second Customer object (which fragments billing history and
    # breaks customer-id-keyed webhook matching). Only fall back to customer_email
    # for a first-time buyer. Passing BOTH is a Stripe error, so it's one or the other.
    existing_cust = user.get("stripe_customer_id") or ""
    customer_kwargs = ({"customer": existing_cust} if existing_cust
                       else {"customer_email": user["email"]})
    try:
        session = stripe.checkout.Session.create(
            mode="subscription",
            payment_method_types=["card"],
            line_items=[{"price": price_id, "quantity": 1}],
            success_url=success_url,
            cancel_url=cancel_url,
            metadata={"user_id": user["id"], "plan": req.plan},
            **customer_kwargs,
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


async def _set_plan_by_customer(cust_id: str, updates: dict, sub_id: str = "") -> None:
    """Apply a $set to the user matched by Stripe customer id (idempotent).

    If the customer-id match updates 0 rows, the user doc is missing the id we
    expect (e.g. checkout.session.completed hasn't landed yet, or the id was
    blanked by an older code path). Rather than silently drop the update, we log
    it and fall back to matching by stripe_subscription_id when one is available —
    and backfill stripe_customer_id so subsequent events match on the fast path.
    """
    if not (cust_id and AUTH_AVAILABLE):
        return
    from auth import get_db
    db = get_db()
    res = await db.users.update_one({"stripe_customer_id": cust_id}, {"$set": updates})
    if res.matched_count:
        return
    print(f"[STRIPE] No user matched customer_id={cust_id}; "
          f"trying subscription_id fallback (sub={sub_id or 'n/a'})")
    if sub_id:
        fb = dict(updates)
        fb["stripe_customer_id"] = cust_id   # backfill so future events match fast
        res2 = await db.users.update_one(
            {"stripe_subscription_id": sub_id}, {"$set": fb})
        if res2.matched_count:
            print(f"[STRIPE] Recovered via subscription_id={sub_id}, "
                  f"backfilled customer_id={cust_id}")
            return
    print(f"[STRIPE] WARNING: no user matched customer_id={cust_id} or "
          f"subscription_id={sub_id or 'n/a'} — update dropped: {list(updates.keys())}")


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
            sub_id  = obj.get("id", "")
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
            await _set_plan_by_customer(cust_id, updates, sub_id=sub_id)
            print(f"[STRIPE] Subscription updated cust={cust_id} status={status} "
                  f"plan={plan or '(unchanged)'} cancel_at_end={cancel_at_end}")

        elif event_type in ("customer.subscription.deleted", "customer.subscription.paused"):
            cust_id = obj.get("customer", "")
            sub_id  = obj.get("id", "")
            await _set_plan_by_customer(cust_id, {
                "plan": "free",
                "subscription_status": "canceled",
                "cancel_at_period_end": False,
            }, sub_id=sub_id)
            print(f"[STRIPE] Subscription ended cust={cust_id} -> downgraded to free")

        elif event_type == "invoice.payment_failed":
            cust_id = obj.get("customer", "")
            sub_id  = obj.get("subscription", "") or ""
            # Don't hard-downgrade — Stripe will retry per the dunning schedule.
            # Flag it so the UI can warn the user; deletion event handles final loss.
            await _set_plan_by_customer(cust_id, {"subscription_status": "past_due"}, sub_id=sub_id)
            print(f"[STRIPE] Payment failed cust={cust_id} -> flagged past_due")

        elif event_type in ("invoice.paid", "invoice.payment_succeeded"):
            cust_id = obj.get("customer", "")
            sub_id  = obj.get("subscription", "") or ""
            await _set_plan_by_customer(cust_id, {"subscription_status": "active"}, sub_id=sub_id)
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
        return_url=f"{get_public_base_url()}/",
    )
    return {"portal_url": session.url}
