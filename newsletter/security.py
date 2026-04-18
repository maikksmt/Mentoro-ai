import hashlib
import logging
from dataclasses import dataclass

from django.core.cache import cache

logger = logging.getLogger(__name__)


class NewsletterAbuseError(Exception):
    pass


class NewsletterEmailTemporarilyUnavailable(Exception):
    pass


@dataclass(frozen=True)
class NewsletterRateLimitConfig:
    ip_window_seconds: int = 600  # 10 Minuten
    ip_max_requests: int = 5  # max. 5 POSTs / 10 Min / IP
    email_cooldown_seconds: int = 3600  # gleiche E-Mail nur 1x pro Stunde
    global_window_seconds: int = 60  # Globales Schutzfenster
    global_max_requests: int = 30  # max. 30 DOI-Mails / Minute insgesamt


RATE_LIMITS = NewsletterRateLimitConfig()


def get_client_ip(request) -> str:
    """
    Erwartet, dass nginx/proxy X-Forwarded-For korrekt setzt.
    Nimmt die erste IP aus XFF, sonst REMOTE_ADDR.
    """
    forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "").strip() or "unknown"


def _increment_or_init(key: str, timeout: int) -> int:
    created = cache.add(key, 1, timeout=timeout)
    if created:
        return 1
    try:
        return cache.incr(key)
    except ValueError:
        cache.set(key, 1, timeout=timeout)
        return 1


def _email_hash(email: str) -> str:
    return hashlib.sha256(email.lower().strip().encode("utf-8")).hexdigest()


def enforce_subscribe_limits(request, email: str) -> None:
    logger.warning("Newsletter security entered")
    ip = get_client_ip(request)
    logger.debug("Checking newsletter limits", extra={"ip": ip, "email": email})
    email_digest = _email_hash(email)

    ip_key = f"newsletter:ip:{ip}"
    email_key = f"newsletter:email:{email_digest}"
    global_key = "newsletter:global"

    ip_count = _increment_or_init(ip_key, RATE_LIMITS.ip_window_seconds)
    if ip_count > RATE_LIMITS.ip_max_requests:
        logger.warning("Newsletter IP rate limit exceeded", extra={"ip": ip, "email": email})
        raise NewsletterAbuseError("ip_rate_limited")

    if cache.get(email_key):
        logger.info("Newsletter email cooldown active", extra={"ip": ip, "email": email})
        raise NewsletterAbuseError("email_cooldown")

    global_count = _increment_or_init(global_key, RATE_LIMITS.global_window_seconds)
    if global_count > RATE_LIMITS.global_max_requests:
        logger.error("Newsletter global rate limit exceeded", extra={"ip": ip, "email": email})
        raise NewsletterEmailTemporarilyUnavailable("global_rate_limited")

    # Cooldown erst nach bestandenem IP/global Check setzen
    cache.set(email_key, 1, timeout=RATE_LIMITS.email_cooldown_seconds)
