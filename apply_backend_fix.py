"""
Backend-side fix for: {"error":"Missing or malformed Authorization header"}
on /api/download/<file>.

Why: a new browser tab (window.open / <a href>) can never send an
Authorization header. This makes the backend remember who you are via a
signed, HttpOnly cookie (set automatically on any authenticated API call)
and accept that cookie ONLY for /api/download. Ownership checks are
unchanged: you can still only download your own reports.

Run from the folder that contains backend/ :
    python apply_backend_fix.py
Backups: backend/auth.py.bak and backend/app.py.bak
"""
import re, shutil, sys, os

base = sys.argv[1] if len(sys.argv) > 1 else "backend"
auth_p, app_p = os.path.join(base, "auth.py"), os.path.join(base, "app.py")

def read(p):
    raw = open(p, "rb").read().decode("utf-8")
    return ("\r\n" if "\r\n" in raw else "\n"), raw.replace("\r\n", "\n")

def write(p, nl, s):
    shutil.copyfile(p, p + ".bak")
    open(p, "wb").write(s.replace("\n", nl).encode("utf-8"))

AUTH_ADD = '''

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
'''

# ---- auth.py
nl, a = read(auth_p)
if "require_auth_or_cookie" not in a:
    a = a.rstrip("\n") + "\n" + AUTH_ADD
    write(auth_p, nl, a)
    print("OK  auth.py patched")
else:
    print("--  auth.py already patched")

# ---- app.py
nl, s = read(app_p)
changed = False
if "require_auth_or_cookie" not in s:
    s, n1 = re.subn(r"^from auth import require_auth\s*$",
                    "from auth import require_auth, require_auth_or_cookie, install_download_cookie",
                    s, count=1, flags=re.M)
    s, n2 = re.subn(
        r"(@app\.route\('/api/download/<filename>'\)\n)@require_auth\n(def download_report)",
        r"install_download_cookie(app)\n\1@require_auth_or_cookie\n\2", s, count=1)
    if n1 and n2:
        write(app_p, nl, s)
        print("OK  app.py patched")
    else:
        print(f"MISSING in app.py (import:{n1} route:{n2}) - nothing written, send me app.py")
        sys.exit(1)
else:
    print("--  app.py already patched")
print("\nRestart Flask, then run an analysis (or just reload the page once so an API call sets the cookie) and click Download Report.")