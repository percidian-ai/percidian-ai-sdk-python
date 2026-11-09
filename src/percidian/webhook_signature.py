"""Checks that a webhook came from Percept.

Percept signs every webhook as Standard Webhooks does: ``webhook-id``, ``webhook-timestamp`` (Unix seconds) and
``webhook-signature``, a space-separated list of ``v1,<base64 HMAC-SHA256 of "<id>.<timestamp>.<body>">``, keyed by the
base64 after ``whsec_`` in the secret. During a secret's rotation a delivery carries a signature for each of the two secrets,
so either one verifies.
"""

import base64
import binascii
import hashlib
import hmac
import json
import time
import typing

TOLERANCE_SECONDS = 5 * 60


class WebhookVerificationError(Exception):
    """The webhook is not signed by this secret, its headers are missing, or its timestamp is more than 5 minutes off."""


def verify_webhook(
    secret: str,
    headers: typing.Mapping[str, str],
    body: typing.Union[str, bytes],
    *,
    now: typing.Optional[float] = None,
) -> typing.Any:
    """Verifies a webhook and returns its body, parsed.

    ``secret`` is the endpoint's ``whsec_`` secret, or the organization's for a request's ``webhook_url``
    (``client.webhooks.get_secret()``). ``headers`` are the request's, in any case; ``body`` its raw body, exactly as received
    (bytes, or text decoded as UTF-8), never re-serialized JSON. Raises WebhookVerificationError if it does not verify.
    """
    by_name = {name.lower(): value for name, value in headers.items()}
    webhook_id = by_name.get("webhook-id")
    timestamp = by_name.get("webhook-timestamp")
    signatures = by_name.get("webhook-signature")
    if not webhook_id or not timestamp or not signatures:
        raise WebhookVerificationError("missing webhook-id, webhook-timestamp or webhook-signature header")
    try:
        sent_at = int(timestamp)
    except ValueError:
        raise WebhookVerificationError("webhook-timestamp is not Unix seconds") from None
    if abs((time.time() if now is None else now) - sent_at) > TOLERANCE_SECONDS:
        raise WebhookVerificationError("webhook-timestamp is more than 5 minutes off")
    try:
        key = base64.b64decode(secret[len("whsec_") :] if secret.startswith("whsec_") else secret, validate=True)
    except (binascii.Error, ValueError):
        raise WebhookVerificationError("the secret is not a whsec_ secret") from None
    raw = body.encode("utf-8") if isinstance(body, str) else body
    expected = base64.b64encode(hmac.new(key, f"{webhook_id}.{timestamp}.".encode() + raw, hashlib.sha256).digest()).decode()
    for signature in signatures.split(" "):
        version, _, value = signature.partition(",")
        if version == "v1" and hmac.compare_digest(value, expected):
            return json.loads(raw)
    raise WebhookVerificationError("no signature matches this secret")
