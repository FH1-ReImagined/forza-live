"""The design's rate limits, as allauth rate limits (settings.ACCOUNT_RATE_LIMITS), kept in the
shared cache. allauth's public consume() skips GET requests; a sign-up happens in the
provider's callback, which is a GET, so every limit here counts GETs too."""

from allauth.core.internal import ratelimit as _impl


def take(request, action, key=None, dry_run=False):
    """True when the action is within its limits (and, unless dry_run, counts it)."""
    return _impl.consume(request, app="allauth.account", action=action, key=key, dry_run=dry_run,
                         limit_get=True) is not None
