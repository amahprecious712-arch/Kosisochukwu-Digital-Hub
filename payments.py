"""
payments.py
-----------
Handles Paystack payment verification via webhooks (NOT frontend redirects).

WHY WEBHOOKS, NOT FRONTEND REDIRECTS:
A frontend redirect (e.g. Paystack sending the browser back to
"/payment/success/") can be faked by ANYONE by just visiting that URL
manually — there's no proof a payment actually happened. Webhooks are
server-to-server: Paystack's servers call YOUR server directly, and we
cryptographically verify the request really came from Paystack using
HMAC SHA512 signature verification. This is the only safe way to
upgrade a paid tier.

FLOW:
1. Student pays on Paystack's checkout page (initiated from your frontend).
2. Paystack's servers send a POST webhook to /api/payments/webhook/
   regardless of what the student's browser does.
3. We verify the signature, confirm the event type, then activate/upgrade
   the Subscription server-side.
4. We ALSO verify the transaction independently via Paystack's Verify API
   (belt-and-braces) before trusting the amount — never trust amount data
   from the webhook payload alone, since amounts can be tampered with in
   transit in theory; verifying server-to-server closes that gap.
"""

import hashlib
import hmac
import json
import logging

import requests
from django.conf import settings
from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django.utils import timezone
from django.db import transaction
from datetime import timedelta

from .models import Subscription

logger = logging.getLogger("payments")

# Map Paystack's plan/amount to your tiers. Amounts are in KOBO (₦1 = 100 kobo).
TIER_AMOUNTS_KOBO = {
    100000: Subscription.Tier.WAEC_NECO,   # ₦1,000
    200000: Subscription.Tier.UNIVERSITY,  # ₦2,000
    300000: Subscription.Tier.JAMB,        # ₦3,000
}

SUBSCRIPTION_DURATION_DAYS = 365  # e.g. valid for 1 academic year — adjust as needed


def _verify_paystack_signature(request) -> bool:
    """
    Confirms this webhook request genuinely came from Paystack.

    Paystack signs every webhook body with your SECRET KEY using HMAC-SHA512
    and sends the result in the `X-Paystack-Signature` header. We recompute
    the same hash on our end using the SAME secret key. If they match, the
    payload is authentic and untampered. If they don't match (or the header
    is missing), REJECT the request immediately — this is the core defense
    against a fake/spoofed "payment successful" webhook.
    """
    signature = request.headers.get("X-Paystack-Signature", "")
    if not signature:
        return False

    computed_hash = hmac.new(
        key=settings.PAYSTACK_SECRET_KEY.encode("utf-8"),
        msg=request.body,  # raw bytes — must be the untouched request body
        digestmod=hashlib.sha512,
    ).hexdigest()

    # hmac.compare_digest prevents "timing attacks" — a naive `==` comparison
    # leaks tiny timing differences that can theoretically help an attacker
    # guess the correct signature byte-by-byte. Always use compare_digest
    # for comparing secrets/signatures.
    return hmac.compare_digest(computed_hash, signature)


def _verify_transaction_with_paystack(reference: str) -> dict | None:
    """
    Belt-and-braces check: independently ask Paystack's Verify Transaction
    API "did this transaction really succeed, and for how much?" rather
    than trusting the webhook payload's numbers alone.

    Docs: GET https://api.paystack.co/transaction/verify/:reference
    """
    url = f"https://api.paystack.co/transaction/verify/{reference}"
    headers = {"Authorization": f"Bearer {settings.PAYSTACK_SECRET_KEY}"}

    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        data = response.json()
    except requests.RequestException as e:
        logger.error(f"Paystack verify API call failed for ref={reference}: {e}")
        return None

    if not data.get("status"):
        return None

    return data.get("data")  # contains status, amount, reference, customer, etc.


@csrf_exempt  # Webhooks come from Paystack's servers, not a browser session —
              # there's no CSRF token to check here. Signature verification
              # (above) is what replaces CSRF protection for this endpoint.
              # NEVER remove csrf_exempt casually elsewhere; this is a
              # deliberate, narrow exception for server-to-server calls only.
@require_POST
def paystack_webhook(request):
    """
    Main webhook endpoint. Register this URL in your Paystack Dashboard
    under Settings > API Keys & Webhooks as:
        https://yourdomain.com/api/payments/webhook/
    """

    # --- Step 1: Verify the request is genuinely from Paystack ---
    if not _verify_paystack_signature(request):
        logger.warning("Rejected webhook: invalid Paystack signature.")
        # Return 400, not 200 — Paystack will retry if it doesn't get a 200,
        # but we don't want to reveal WHY it failed via response content.
        return HttpResponse(status=400)

    try:
        payload = json.loads(request.body)
    except json.JSONDecodeError:
        return HttpResponse(status=400)

    event = payload.get("event")

    # --- Step 2: Only act on the event we care about ---
    if event != "charge.success":
        # Acknowledge other event types (e.g. transfer events) without acting.
        return HttpResponse(status=200)

    data = payload.get("data", {})
    reference = data.get("reference")

    if not reference:
        return HttpResponse(status=400)

    # --- Step 3: Independently re-verify with Paystack (don't trust payload alone) ---
    verified_data = _verify_transaction_with_paystack(reference)
    if not verified_data or verified_data.get("status") != "success":
        logger.warning(f"Webhook claimed success but verify API disagreed: ref={reference}")
        return HttpResponse(status=400)

    amount_kobo = verified_data.get("amount")
    customer_email = verified_data.get("customer", {}).get("email")

    tier = TIER_AMOUNTS_KOBO.get(amount_kobo)
    if not tier:
        logger.warning(f"Webhook amount {amount_kobo} doesn't match any known tier.")
        return HttpResponse(status=400)

    # --- Step 4: Prevent double-processing the same payment (idempotency) ---
    # If Paystack retries this webhook (they do, on network hiccups), we must
    # not upgrade the user twice for one payment. `payment_reference` is
    # unique on the model, so we check first.
    if Subscription.objects.filter(payment_reference=reference).exists():
        logger.info(f"Webhook for ref={reference} already processed — skipping.")
        return HttpResponse(status=200)

    # --- Step 5: Activate the subscription, atomically ---
    from django.contrib.auth import get_user_model
    User = get_user_model()

    try:
        user = User.objects.get(email=customer_email)
    except User.DoesNotExist:
        logger.error(f"No matching user for email={customer_email}, ref={reference}")
        return HttpResponse(status=400)

    with transaction.atomic():
        Subscription.objects.create(
            user=user,
            tier=tier,
            status=Subscription.Status.ACTIVE,
            amount_paid=amount_kobo / 100,  # convert kobo back to naira
            payment_reference=reference,
            start_date=timezone.now(),
            end_date=timezone.now() + timedelta(days=SUBSCRIPTION_DURATION_DAYS),
        )

    logger.info(f"Activated {tier} subscription for {user.username} (ref={reference})")
    return HttpResponse(status=200)


def initialize_payment(request):
    """
    Called from your frontend when a student clicks "Pay Now."
    This talks to Paystack's Initialize Transaction API to get a checkout
    URL — the ACTUAL upgrade only ever happens later, in the webhook above.

    Expects POST body: { "tier": "JAMB" | "WAEC_NECO" | "UNIVERSITY" }
    """
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)

    if not request.user.is_authenticated:
        return JsonResponse({"error": "Login required"}, status=401)

    try:
        body = json.loads(request.body)
        tier_choice = body.get("tier")
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    tier_to_amount_naira = {
        Subscription.Tier.WAEC_NECO: 1000,
        Subscription.Tier.UNIVERSITY: 2000,
        Subscription.Tier.JAMB: 3000,
    }

    if tier_choice not in tier_to_amount_naira:
        return JsonResponse({"error": "Invalid tier"}, status=400)

    amount_kobo = tier_to_amount_naira[tier_choice] * 100

    url = "https://api.paystack.co/transaction/initialize"
    headers = {
        "Authorization": f"Bearer {settings.PAYSTACK_SECRET_KEY}",
        "Content-Type": "application/json",
    }
    body_data = {
        "email": request.user.email,
        "amount": amount_kobo,
        "callback_url": settings.PAYSTACK_CALLBACK_URL,  # frontend "thank you" page
        # metadata is informational only — NEVER trusted for the actual
        # upgrade logic. The webhook + verify API are the source of truth.
        "metadata": {"tier": tier_choice, "user_id": request.user.id},
    }

    try:
        response = requests.post(url, headers=headers, json=body_data, timeout=10)
        response.raise_for_status()
        result = response.json()
    except requests.RequestException as e:
        logger.error(f"Paystack initialize failed: {e}")
        return JsonResponse({"error": "Payment initialization failed"}, status=502)

    return JsonResponse({
        "authorization_url": result["data"]["authorization_url"],
        "reference": result["data"]["reference"],
    })