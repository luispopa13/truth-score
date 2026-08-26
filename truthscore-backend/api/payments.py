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
    try:
        session = stripe.checkout.Session.create(
            mode="subscription",
            payment_method_types=["card"],
            line_items=[{"price": price_id, "quantity": 1}],
            success_url=req.success_url + "&session_id={CHECKOUT_SESSION_ID}",
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


async def stripe_webhook(request: Request):
    """Handle Stripe webhooks -- upgrade user plan on payment."""
    if not STRIPE_AVAILABLE:
        return {"status": "stripe not configured"}
    payload = await request.body()
    sig     = request.headers.get("stripe-signature", "")
    try:
        event = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
    except Exception as e:
        raise HTTPException(400, str(e))

    if event["type"] == "checkout.session.completed":
        session  = event["data"]["object"]
        user_id  = session.get("metadata", {}).get("user_id")
        plan     = session.get("metadata", {}).get("plan", "pro")
        cust_id  = session.get("customer", "")
        sub_id   = session.get("subscription", "")
        if user_id:
            await upgrade_user_plan(user_id, plan, cust_id, sub_id)

    if event["type"] in ("customer.subscription.deleted", "customer.subscription.paused"):
        sub = event["data"]["object"]
        cust_id = sub.get("customer", "")
        if cust_id and AUTH_AVAILABLE:
            from auth import get_db
            db = get_db()
            await db.users.update_one(
                {"stripe_customer_id": cust_id},
                {"$set": {"plan": "free"}}
            )
    return {"status": "ok"}


async def customer_portal(user=Depends(require_user)):
    """Redirect user to Stripe billing portal."""
    if not STRIPE_AVAILABLE:
        raise HTTPException(503, "Stripe not configured")
    cust_id = user.get("stripe_customer_id")
    if not cust_id:
        raise HTTPException(400, "No subscription found")
    session = stripe.billing_portal.Session.create(
        customer=cust_id,
        return_url="http://localhost:8000/app",
    )
    return {"portal_url": session.url}


# [U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550]
# BATCH VERIFY
# [U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550][U2550]

class BatchVerifyRequest(BaseModel):
    claims: list[str]

class BatchVerifyResponse(BaseModel):
    results: list[dict]
    total: int
    success: int
    failed: int


async def get_plans():
    """Return available pricing plans — single source of truth from auth.PLANS."""
    from auth import PLANS
    return {
        name: {
            "name": p.get("name", name),
            "price": p.get("price", 0),
            "daily_limit": p.get("daily_limit", 0),
            "batch": p.get("batch_limit", 0) > 0,
            "pdf": p.get("pdf", False),
            "widget": p.get("widget", False),
            "features": p.get("features", {}),
        }
        for name, p in PLANS.items()
    }


class GoogleAuthRequest(BaseModel):
    token: str  # Google access token from chrome.identity