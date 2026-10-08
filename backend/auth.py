"""
Server-side Firebase auth enforcement.

Before this file existed, login.html did all "auth" entirely client-side:
it signed the user into Firebase, wrote a profile to Firestore, and
stashed a display name in localStorage. It never sent anything to Flask,
and Flask never checked anything. Every /api/* route trusted whatever the
request body claimed — meaning anyone could `curl` the API directly,
skip the login page entirely, and run unlimited analyses or read other
users' data. This module closes that gap.

Usage in app.py:

    from auth import require_auth
    from flask import g

    @app.route('/api/analyze', methods=['POST'])
    @require_auth
    def analyze():
        uid = g.uid          # verified Firebase UID, not client-supplied
        ...
"""
import functools
import json
import os
import time

import firebase_admin
from firebase_admin import auth as firebase_auth
from firebase_admin import credentials
from flask import g, jsonify, request

_initialized = False

# Firebase's verify_id_token() compares the token's "issued at" / "not
# before" claims against THIS server's clock with zero tolerance by
# default (clock_skew_seconds=0). Any drift between the client's clock
# (which stamped the token) and this machine's clock — even a fraction of
# a second, as in the "Token used too early" failures this module used to
# log — makes a perfectly valid token look invalid and every request gets
# rejected with a 401. This gives a small, fixed allowance for that drift
# without weakening expiry/signature/revocation checks (those are
# unaffected by this value).
_CLOCK_SKEW_SECONDS = 10


def _init_firebase():
    global _initialized
    if _initialized:
        return
    # Prefer a service-account JSON supplied via env var (Render, most PaaS
    # providers). Fall back to GOOGLE_APPLICATION_CREDENTIALS pointing at a
    # file for local dev. Never commit the service-account file itself.
    raw = os.environ.get('FIREBASE_SERVICE_ACCOUNT_JSON')
    if raw:
        cred = credentials.Certificate(json.loads(raw))
        firebase_admin.initialize_app(cred)
    elif os.environ.get('GOOGLE_APPLICATION_CREDENTIALS'):
        firebase_admin.initialize_app()
    else:
        raise RuntimeError(
            "No Firebase service-account credentials configured. Set "
            "FIREBASE_SERVICE_ACCOUNT_JSON (the JSON contents, as a single "
            "env var) or GOOGLE_APPLICATION_CREDENTIALS (a file path) "
            "before starting the app. Download the service account key "
            "from Firebase console → Project settings → Service accounts."
        )
    _initialized = True


def _log_auth_failure(exc):
    """Print the real cause of a failed token verification to the server
    console, plus a one-line hint for the most common causes, so 401s in
    the access log stop being a black box."""
    kind = type(exc).__name__
    hint = None
    if isinstance(exc, firebase_auth.ExpiredIdTokenError):
        hint = ("Token really is expired — the browser held onto an old "
                "one. In firebase-auth.js call user.getIdToken(true) to "
                "force a refresh instead of using a cached token.")
    elif isinstance(exc, firebase_auth.RevokedIdTokenError):
        hint = "Token was revoked (password change / manual revoke / sign-out elsewhere)."
    elif isinstance(exc, firebase_auth.CertificateFetchError):
        hint = ("Could not fetch Google's public signing certs — this "
                "usually means the machine running Flask has no outbound "
                "network access to googleapis.com (firewall/proxy/sandbox), "
                "not that the token itself is bad.")
    elif isinstance(exc, firebase_auth.InvalidIdTokenError):
        skew = time.time()
        hint = (f"Token failed signature/claims validation. Server clock "
                f"reads {skew:.0f} (unix time) — if that's noticeably off "
                f"from real UTC, fix system time; a clock more than "
                f"~{_CLOCK_SKEW_SECONDS}s off makes every valid token look "
                f"invalid even with the clock_skew_seconds allowance below. "
                f"Also double-check this is the SAME Firebase project as "
                f"the one in frontend/static/js/firebase-auth.js's "
                f"firebaseConfig (project_id in serviceAccountKey.json "
                f"must match).")
    print(f"[auth] token verification failed: {kind}: {exc}"
          + (f"\n[auth]   hint: {hint}" if hint else ""))


def require_auth(view_func):
    """
    Verifies the Firebase ID token sent as `Authorization: Bearer <token>`.
    On success, sets g.uid and g.user_email and calls the wrapped view.
    On failure, returns 401 — the wrapped view never runs.
    """
    @functools.wraps(view_func)
    def wrapped(*args, **kwargs):
        _init_firebase()
        header = request.headers.get('Authorization', '')
        if not header.startswith('Bearer '):
            return jsonify({'error': 'Missing or malformed Authorization header'}), 401
        id_token = header[len('Bearer '):].strip()
        try:
            decoded = firebase_auth.verify_id_token(
                id_token, clock_skew_seconds=_CLOCK_SKEW_SECONDS
            )
        except Exception as exc:
            # The client always gets the same generic 401 (no info
            # leakage), but we log the *real* reason server-side —
            # otherwise every distinct failure mode (expired token,
            # wrong project, clock skew, blocked cert fetch, revoked
            # token) looks identical from the terminal and is
            # impossible to debug.
            _log_auth_failure(exc)
            return jsonify({'error': 'Invalid or expired auth token'}), 401
        g.uid = decoded['uid']
        g.user_email = decoded.get('email', '')
        return view_func(*args, **kwargs)
    return wrapped


# ───────────────────────────────────────────────────────────────────────
# Download cookie: lets a plain browser tab (window.open / <a href>) fetch
# /api/download/<file>, which cannot carry an Authorization header.
# The cookie only ever identifies the user for the download route.
# Set DOWNLOAD_COOKIE_SECRET in the env if you run several workers.
# ───────────────────────────────────────────────────────────────────────
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

_DL_COOKIE = 'rl_dl'
_DL_MAX_AGE = 8 * 3600
_dl_serializer = URLSafeTimedSerializer(
    os.environ.get('DOWNLOAD_COOKIE_SECRET') or os.urandom(32).hex(),
    salt='rl-download-cookie')


def _read_download_cookie():
    raw = request.cookies.get(_DL_COOKIE)
    if not raw:
        return None
    try:
        return _dl_serializer.loads(raw, max_age=_DL_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None


def install_download_cookie(app):
    """After any authenticated API call, remember the verified uid in a
    signed HttpOnly cookie."""
    @app.after_request
    def _set_dl_cookie(resp):
        uid = g.get('uid')
        if uid and request.path.startswith('/api/') and _read_download_cookie() != uid:
            resp.set_cookie(_DL_COOKIE, _dl_serializer.dumps(uid),
                            max_age=_DL_MAX_AGE, httponly=True,
                            samesite='Lax', path='/')
        return resp
    return app


def require_auth_or_cookie(view_func):
    """Bearer token if present (same as require_auth); otherwise fall back
    to the signed download cookie."""
    bearer_wrapped = require_auth(view_func)

    @functools.wraps(view_func)
    def wrapped(*args, **kwargs):
        if request.headers.get('Authorization', '').startswith('Bearer '):
            return bearer_wrapped(*args, **kwargs)
        uid = _read_download_cookie()
        if not uid:
            return jsonify({'error': 'Missing or malformed Authorization header'}), 401
        g.uid = uid
        g.user_email = ''
        return view_func(*args, **kwargs)
    return wrapped
