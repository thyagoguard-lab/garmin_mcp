"""
Workaround for garminconnect==0.3.2's widget+cffi login strategy not
recognizing Garmin's email-based MFA challenge page.

Garmin's SSO widget shows a page titled "GARMIN Authentication Application"
for accounts with email-based MFA enabled, instead of the "...MFA code..."
title the library already knows how to handle. Without this patch, login
fails even with correct credentials:

    GarminConnectConnectionError: Widget login: unexpected title
    'GARMIN Authentication Application'

Same root cause reported upstream, unresolved as of writing:
- https://github.com/Taxuspt/garmin_mcp/issues/109
- https://github.com/matin/garth/issues/93

This module monkeypatches garminconnect.client.Client._widget_web_login at
import time so that title is also treated as an MFA prompt (same code path
already used for titles containing "MFA"), instead of failing with
"unexpected title". (Note: the public `Garmin` class in garminconnect's
top-level __init__.py delegates login to a `Client` instance held as
`Garmin.client` — the login logic itself lives in `garminconnect.client.Client`.)
"""

from __future__ import annotations

import random
import re
import time

from garminconnect import client as _gc_client
from garminconnect.exceptions import (
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)

# The exact page title Garmin returns for email-based MFA challenges.
_EMAIL_MFA_TITLE = "GARMIN Authentication Application"

_patched = False


def _patched_widget_web_login(self, email: str, password: str) -> None:
    """Drop-in replacement for garminconnect.client.Client._widget_web_login.

    Identical to the upstream implementation except it also recognizes the
    "GARMIN Authentication Application" page (email MFA challenge) as an
    MFA prompt instead of an unexpected-title failure.
    """
    if not _gc_client.HAS_CFFI:
        raise GarminConnectConnectionError("curl_cffi not available")
    sess = _gc_client.cffi_requests.Session(impersonate="chrome", timeout=30)
    sso_base = f"{self._sso}/sso"
    sso_embed = f"{sso_base}/embed"
    embed_params = {
        "id": "gauth-widget",
        "embedWidget": "true",
        "gauthHost": sso_base,
    }
    signin_params = {
        **embed_params,
        "gauthHost": sso_embed,
        "service": sso_embed,
        "source": sso_embed,
        "redirectAfterAccountLoginUrl": sso_embed,
        "redirectAfterAccountCreationUrl": sso_embed,
    }

    # Step 1: GET embed page to establish session cookies
    r = sess.get(sso_embed, params=embed_params)
    if r.status_code == 429:
        raise GarminConnectTooManyRequestsError("Widget embed GET returned 429")
    if not r.ok:
        raise GarminConnectConnectionError(f"Widget embed returned {r.status_code}")

    # Step 2: GET signin page for CSRF token
    r = sess.get(
        f"{sso_base}/signin",
        params=signin_params,
        headers={"Referer": sso_embed},
    )
    if r.status_code == 429:
        raise GarminConnectTooManyRequestsError("Widget signin GET returned 429")

    csrf_match = _gc_client._CSRF_RE.search(r.text)
    if not csrf_match:
        raise GarminConnectConnectionError("Widget login: missing CSRF token")

    # Anti-WAF delay between GET and POST
    delay_s = random.uniform(  # noqa: S311
        _gc_client.WIDGET_DELAY_MIN_S, _gc_client.WIDGET_DELAY_MAX_S
    )
    time.sleep(delay_s)

    # Step 3: POST credentials
    r = sess.post(
        f"{sso_base}/signin",
        params=signin_params,
        headers={"Referer": r.url},
        data={
            "username": email,
            "password": password,
            "embed": "true",
            "_csrf": csrf_match.group(1),
        },
        timeout=30,
    )

    if r.status_code == 429:
        raise GarminConnectTooManyRequestsError("Widget signin POST returned 429")

    title_match = _gc_client._TITLE_RE.search(r.text)
    title = title_match.group(1) if title_match else ""
    title_lower = title.lower()

    # Detect server/infrastructure errors — fall through to next strategy
    if any(
        hint in title_lower
        for hint in ("bad gateway", "service unavailable", "cloudflare", "502", "503")
    ):
        raise GarminConnectConnectionError(f"Widget login: server error '{title}'")

    # Early credential detection — don't waste remaining strategies
    if any(
        hint in title_lower
        for hint in ("locked", "invalid", "incorrect", "account error")
    ):
        raise GarminConnectAuthenticationError(f"Widget authentication failed: '{title}'")

    # --- the actual fix: also treat the email-MFA challenge page as MFA ---
    if "MFA" in title or title.strip() == _EMAIL_MFA_TITLE:
        self._mfa_session = sess
        self._mfa_login_params = signin_params
        self._mfa_post_headers = {"Referer": r.url}
        self._mfa_flow = "widget"
        self._widget_last_resp = r
        raise _gc_client._MFARequired()

    if title != "Success":
        raise GarminConnectConnectionError(f"Widget login: unexpected title '{title}'")

    # Step 4: Extract service ticket
    ticket_match = re.search(r'embed\?ticket=([^"]+)"', r.text)
    if not ticket_match:
        raise GarminConnectConnectionError("Widget login: missing service ticket")

    self._establish_session(ticket_match.group(1), sess=sess, service_url=sso_embed)


def apply() -> None:
    """Monkeypatch Client._widget_web_login with the MFA-title fix.

    Idempotent — safe to call more than once (e.g. from both auth_cli.py
    and the server entrypoint).
    """
    global _patched
    if _patched:
        return
    _gc_client.Client._widget_web_login = _patched_widget_web_login
    _patched = True
