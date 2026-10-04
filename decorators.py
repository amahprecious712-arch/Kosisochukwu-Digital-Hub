"""
decorators.py
-------------
Access-control decorators/mixins that gate paid content behind:
  1. Authentication (is the user logged in at all?)
  2. An ACTIVE subscription of the RIGHT tier (not just any payment ever made)

Use these on every view that touches paid content — especially the
University PDF upload / AI generation views, which are the most
resource-expensive (and therefore most important to lock down tightly).

We provide BOTH a function-based decorator AND a class-based Mixin,
since Django projects often mix function-based and class-based views.
"""

from functools import wraps
from django.http import JsonResponse
from django.contrib.auth.decorators import login_required

from models import Subscription


def tier_required(*allowed_tiers):
    """
    Function-based view decorator.

    Usage:
        @tier_required(Subscription.Tier.UNIVERSITY)
        def upload_course_material(request):
            ...

        @tier_required(Subscription.Tier.JAMB, Subscription.Tier.WAEC_NECO)
        def some_shared_view(request):
            ...

    IMPORTANT SECURITY NOTE (Insecure Direct Object Reference / IDOR):
    This decorator only checks "does this user have an active subscription
    of the right tier?" — it does NOT check "does this user own the specific
    object they're requesting?" (e.g. a CourseMaterial ID in the URL).
    You must ALSO filter querysets by `request.user` in the view itself
    (shown in views.py) to prevent User A from accessing User B's
    materials just by guessing/incrementing an ID in the URL.
    """
    def decorator(view_func):
        @wraps(view_func)
        @login_required  # redirects anonymous users to LOGIN_URL automatically
        def wrapped_view(request, *args, **kwargs):
            from django.utils import timezone

            # Single query: status must be ACTIVE in the DB, of the right
            # tier, AND not past its end_date. We check the real clock here
            # rather than trusting stored status alone, since a subscription
            # can remain "ACTIVE" in the DB after its end_date if a cron job
            # hasn't yet run to flip expired subs to EXPIRED.
            has_access = Subscription.objects.filter(
                user=request.user,
                tier__in=allowed_tiers,
                status=Subscription.Status.ACTIVE,
            ).filter(
                models_end_date_ok(timezone.now())
            ).exists()

            if not has_access:
                return JsonResponse(
                    {"error": "An active subscription for this feature is required."},
                    status=403  # 403 Forbidden: authenticated, but not authorized
                )

            return view_func(request, *args, **kwargs)
        return wrapped_view
    return decorator


def models_end_date_ok(now):
    """Small helper building the Q object for 'not expired' checks, kept
    separate so it's easy to test/reuse."""
    from django.db.models import Q
    return Q(end_date__isnull=True) | Q(end_date__gte=now)


class TierRequiredMixin:
    """
    Class-based view mixin equivalent of `tier_required`.

    Usage:
        class UploadCourseMaterialView(TierRequiredMixin, View):
            allowed_tiers = [Subscription.Tier.UNIVERSITY]

            def post(self, request):
                ...

    Put TierRequiredMixin FIRST in the parent class list — Python's MRO
    (method resolution order) means dispatch() runs left-to-right, so the
    access check must come before Django's base View.dispatch().
    """
    allowed_tiers = []  # override in subclasses

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return JsonResponse({"error": "Authentication required."}, status=401)

        from django.utils import timezone
        has_access = Subscription.objects.filter(
            user=request.user,
            tier__in=self.allowed_tiers,
            status=Subscription.Status.ACTIVE,
        ).filter(
            models_end_date_ok(timezone.now())
        ).exists()

        if not has_access:
            return JsonResponse(
                {"error": "An active subscription for this feature is required."},
                status=403
            )

        return super().dispatch(request, *args, **kwargs)
