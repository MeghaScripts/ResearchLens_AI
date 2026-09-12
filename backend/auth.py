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

import firebase_admin
from firebase_admin import auth as firebase_auth
from firebase_admin import credentials
from flask import g, jsonify, request

_initialized = False


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
            decoded = firebase_auth.verify_id_token(id_token)
        except Exception:
            # Covers expired tokens, tampered tokens, revoked tokens, and
            # tokens issued for a different Firebase project — all of
            # which must be rejected the same way (no info leakage).
            return jsonify({'error': 'Invalid or expired auth token'}), 401
        g.uid = decoded['uid']
        g.user_email = decoded.get('email', '')
        return view_func(*args, **kwargs)
    return wrapped
