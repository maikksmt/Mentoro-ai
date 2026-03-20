import logging
import smtplib
import socket

from django.conf import settings
from django.core.mail import EmailMultiAlternatives, get_connection
from django.core.signing import TimestampSigner
from django.template.loader import render_to_string
from django.urls import reverse

from newsletter.security import NewsletterEmailTemporarilyUnavailable
from .models import Subscriber

logger = logging.getLogger(__name__)


def build_unsubscribe_url(subscriber: Subscriber, request) -> str:
    signer = TimestampSigner()
    payload = {"email": subscriber.email}
    token = signer.sign_object(payload)
    return request.build_absolute_uri(reverse("newsletter:unsubscribe_confirm", args=[token]))


def send_double_opt_in_email(subscriber: Subscriber, request) -> bool:
    token = subscriber.refresh_doi_token()
    confirmation_url = request.build_absolute_uri(
        reverse("newsletter:confirm", args=[token])
    )
    unsubscribe_url = build_unsubscribe_url(subscriber, request)

    context = {
        "subscriber": subscriber,
        "confirmation_url": confirmation_url,
        "unsubscribe_url": unsubscribe_url,
    }

    subject = render_to_string("newsletter/emails/confirm_subject.txt", context).strip()
    text_body = render_to_string("newsletter/emails/confirm_body.txt", context)
    html_body = render_to_string("newsletter/emails/confirm_body.html", context)

    connection = get_connection(timeout=10)

    message = EmailMultiAlternatives(
        subject=subject,
        body=text_body,
        from_email=getattr(settings, "DEFAULT_FROM_EMAIL", None),
        to=[subscriber.email],
        connection=connection,
        headers={
            "X-Entity-Ref-ID": f"newsletter-doi:{subscriber.pk}",
        },
    )
    message.attach_alternative(html_body, "text/html")

    try:
        sent = message.send(fail_silently=False)
        return bool(sent)
    except (
            smtplib.SMTPException,
            socket.timeout,
            TimeoutError,
            OSError,
    ) as exc:
        logger.exception(
            "Newsletter DOI email sending failed",
            extra={
                "subscriber_id": subscriber.pk,
                "email": subscriber.email,
                "path": request.path,
            },
        )
        raise NewsletterEmailTemporarilyUnavailable("smtp_send_failed") from exc
