"""Signed links for attorneys: an emailed link is all an attorney needs to accept or decline.

The token is an HMAC "signature" of the offer id using a server secret. Nothing
is stored; anyone without the secret can't forge a valid link, and changing
one character of the offer id breaks the signature.
"""

from __future__ import annotations

import hashlib
import hmac


def sign_offer(secret: str, offer_id: str) -> str:
    return hmac.new(secret.encode(), f"offer:{offer_id}".encode(), hashlib.sha256).hexdigest()[:32]


def verify_offer(secret: str, offer_id: str, token: str) -> bool:
    # Bytes, not str: compare_digest raises on non-ASCII text, which would turn a
    # bad link into a 500 and reveal which offer ids exist.
    expected = sign_offer(secret, offer_id).encode()
    return hmac.compare_digest(expected, (token or "").encode("utf-8", "replace"))


def offer_link(base_url: str, secret: str, offer_id: str) -> str:
    """The token goes after '#', which browsers never send to the server, so it can't
    end up in access logs. The page reads it and sends it in a header."""
    return f"{base_url.rstrip('/')}/offer/{offer_id}#t={sign_offer(secret, offer_id)}"
