"""
Admin API for frontend/admin.html  (/api/admin/*).

Env vars required:
    ADMIN_EMAIL      e.g. megham112004@gmail.com
    ADMIN_PASSWORD   the password you type on the admin sign-in page
    APP_SECRET_KEY   (already required by crypto_utils.py) - signs admin tokens

Users live in Firebase Auth, so this lists them via the Firebase Admin SDK.
Plan ("free"/"premium") is stored in SQLite; "suspended" disables the
Firebase account so the user really cannot sign in.
"""
import datetime
import functools
import hmac
import os

from flask import Blueprint, jsonify, request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

bp = Blueprint('admin_api', __name__, url_prefix='/api/admin')

_get_db = None
_paths = {}
_TOKEN_MAX_AGE = 8 * 3600


def _serializer():
    return URLSafeTimedSerializer(os.environ['APP_SECRET_KEY'], salt='rl-admin')


def _now():
    return datetime.datetime.now().isoformat()


def _init_tables():
    conn = _get_db()
    conn.execute('''CREATE TABLE IF NOT EXISTS api_calls (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        endpoint TEXT, method TEXT, status INTEGER, created_at TEXT)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS error_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        endpoint TEXT, error_message TEXT, user_id TEXT, created_at TEXT)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS admin_user_meta (
        uid TEXT PRIMARY KEY, plan TEXT DEFAULT 'free')''')
    conn.commit()
    conn.close()


def require_admin(fn):
    @functools.wraps(fn)
    def wrapped(*a, **kw):
        header = request.headers.get('Authorization', '')
        if not header.startswith('Bearer '):
            return jsonify({'error': 'Not authorized'}), 401
        try:
            data = _serializer().loads(header[7:].strip(), max_age=_TOKEN_MAX_AGE)
        except (BadSignature, SignatureExpired):
            return jsonify({'error': 'Not authorized'}), 401
        if data.get('role') != 'admin':
            return jsonify({'error': 'Not authorized'}), 403
        return fn(*a, **kw)
    return wrapped


# ── Firebase helpers ───────────────────────────────────────────────
def _fb():
    from auth import _init_firebase
    from firebase_admin import auth as fb_auth
    _init_firebase()
    return fb_auth


def _all_firebase_users():
    fb = _fb()
    out, page = [], fb.list_users()
    while page:
        out.extend(page.users)
        page = page.get_next_page()
    return out


def _user_lookup():
    try:
        return {u.uid: u for u in _all_firebase_users()}
    except Exception as e:
        print(f'[admin] Firebase user lookup failed: {e}')
        return {}


def _iso_ms(ms):
    if not ms:
        return ''
    return datetime.datetime.fromtimestamp(ms / 1000).isoformat()


# ── Auth ───────────────────────────────────────────────────────────
@bp.route('/login', methods=['POST'])
def login():
    admin_email = os.environ.get('ADMIN_EMAIL', '').strip().lower()
    admin_pass = os.environ.get('ADMIN_PASSWORD', '')
    if not admin_email or not admin_pass:
        return jsonify({'error': 'ADMIN_EMAIL / ADMIN_PASSWORD are not set on the server'}), 500
    body = request.get_json(silent=True) or {}
    email = str(body.get('email', '')).strip().lower()
    password = str(body.get('password', ''))
    ok = hmac.compare_digest(email.encode(), admin_email.encode()) & \
         hmac.compare_digest(password.encode(), admin_pass.encode())
    if not ok:
        return jsonify({'error': 'Invalid email or password'}), 401
    return jsonify({'token': _serializer().dumps({'role': 'admin', 'email': email})})


# ── Users ──────────────────────────────────────────────────────────
@bp.route('/users', methods=['GET'])
@require_admin
def users():
    try:
        fb_users = _all_firebase_users()
    except Exception as e:
        return jsonify({'error': f'Could not read Firebase users: {e}'}), 500
    conn = _get_db()
    plans = {r['uid']: r['plan'] for r in conn.execute('SELECT uid, plan FROM admin_user_meta')}
    conn.close()
    admin_email = os.environ.get('ADMIN_EMAIL', '').strip().lower()
    rows = [{
        'id': u.uid,
        'name': u.display_name or '',
        'email': u.email or '',
        'plan': plans.get(u.uid, 'free'),
        'status': 'suspended' if u.disabled else 'active',
        'is_admin': (u.email or '').lower() == admin_email,
        'created_at': _iso_ms(u.user_metadata.creation_timestamp),
    } for u in fb_users]
    rows.sort(key=lambda r: r['created_at'], reverse=True)
    return jsonify({'users': rows})


@bp.route('/users/<uid>', methods=['PATCH'])
@require_admin
def patch_user(uid):
    body = request.get_json(silent=True) or {}
    if 'plan' in body:
        if body['plan'] not in ('free', 'premium'):
            return jsonify({'error': 'plan must be free or premium'}), 400
        conn = _get_db()
        conn.execute('INSERT INTO admin_user_meta (uid, plan) VALUES (?, ?) '
                     'ON CONFLICT(uid) DO UPDATE SET plan=excluded.plan', (uid, body['plan']))
        conn.commit()
        conn.close()
    if 'status' in body:
        if body['status'] not in ('active', 'suspended'):
            return jsonify({'error': 'status must be active or suspended'}), 400
        try:
            _fb().update_user(uid, disabled=(body['status'] == 'suspended'))
        except Exception as e:
            return jsonify({'error': str(e)}), 500
    return jsonify({'ok': True})


@bp.route('/users/<uid>', methods=['DELETE'])
@require_admin
def delete_user(uid):
    try:
        _fb().delete_user(uid)
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    conn = _get_db()
    conn.execute('DELETE FROM admin_user_meta WHERE uid=?', (uid,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


# ── Papers / API keys / storage ────────────────────────────────────
@bp.route('/papers', methods=['GET'])
@require_admin
def papers():
    conn = _get_db()
    rows = conn.execute('SELECT id, topic, gaps_count, COALESCE(owner_uid, user_id) AS uid, created_at '
                        'FROM analyses ORDER BY id DESC LIMIT 500').fetchall()
    conn.close()
    return jsonify({'papers': [{'id': r['id'], 'topic': r['topic'], 'gaps_count': r['gaps_count'] or 0,
                                'user_id': r['uid'], 'created_at': r['created_at']} for r in rows]})


@bp.route('/api-keys', methods=['GET'])
@require_admin
def api_keys():
    from crypto_utils import decrypt_secret
    lookup = _user_lookup()
    conn = _get_db()
    rows = conn.execute("SELECT owner_uid, api_key FROM watched_topics "
                        "WHERE api_key IS NOT NULL AND api_key != ''").fetchall()
    conn.close()
    seen, out = set(), []
    for r in rows:
        key = decrypt_secret(r['api_key'])
        if not key or (r['owner_uid'], key) in seen:
            continue
        seen.add((r['owner_uid'], key))
        u = lookup.get(r['owner_uid'])
        out.append({'user_id': r['owner_uid'] or '-', 'name': (u.display_name if u else '') or '',
                    'email': (u.email if u else '') or '',
                    'masked_key': key[:4] + '…' + key[-4:] if len(key) > 8 else '••••'})
    return jsonify({'api_keys': out})


def _dir_stats(path):
    files, size = 0, 0
    if os.path.isdir(path):
        for n in os.listdir(path):
            p = os.path.join(path, n)
            if os.path.isfile(p):
                files += 1
                size += os.path.getsize(p)
    return {'files': files, 'mb': round(size / 1048576, 2)}


@bp.route('/storage', methods=['GET'])
@require_admin
def storage():
    db = _paths['db']
    db_mb = round(os.path.getsize(db) / 1048576, 2) if os.path.exists(db) else 0
    return jsonify({'uploads': _dir_stats(_paths['uploads']), 'reports': _dir_stats(_paths['reports']),
                    'database_mb': db_mb})


# ── Analytics / feedback / errors ──────────────────────────────────
@bp.route('/analytics', methods=['GET'])
@require_admin
def analytics():
    conn = _get_db()
    total_analyses = conn.execute('SELECT COUNT(*) FROM analyses').fetchone()[0]
    total_calls = conn.execute('SELECT COUNT(*) FROM api_calls').fetchone()[0]
    by_ep = conn.execute('SELECT endpoint, COUNT(*) AS c FROM api_calls GROUP BY endpoint '
                         'ORDER BY c DESC LIMIT 20').fetchall()
    since = (datetime.date.today() - datetime.timedelta(days=13)).isoformat()
    by_day = conn.execute('SELECT substr(created_at,1,10) AS d, COUNT(*) AS c FROM api_calls '
                          'WHERE created_at >= ? GROUP BY d ORDER BY d DESC', (since,)).fetchall()
    premium = conn.execute("SELECT COUNT(*) FROM admin_user_meta WHERE plan='premium'").fetchone()[0]
    conn.close()
    try:
        total_users = len(_all_firebase_users())
    except Exception:
        total_users = 0
    return jsonify({'total_users': total_users, 'premium_users': premium,
                    'total_analyses': total_analyses, 'total_api_calls': total_calls,
                    'calls_by_endpoint': [dict(r) for r in by_ep],
                    'calls_last_14_days': [dict(r) for r in by_day]})


@bp.route('/feedback', methods=['GET'])
@require_admin
def feedback():
    conn = _get_db()
    rows = conn.execute('SELECT * FROM gap_feedback ORDER BY rated_at DESC LIMIT 200').fetchall()
    conn.close()
    return jsonify({'feedback': [{
        'name': r['rater_name'] or 'anonymous', 'email': '',
        'rating': r['confidence_score'],
        'message': f"[{r['verdict']}] {r['gap_title']}" + (f" — {r['notes']}" if r['notes'] else ''),
        'created_at': r['rated_at']} for r in rows]})


@bp.route('/error-logs', methods=['GET'])
@require_admin
def error_logs():
    conn = _get_db()
    rows = conn.execute('SELECT * FROM error_logs ORDER BY id DESC LIMIT 200').fetchall()
    conn.close()
    return jsonify({'error_logs': [dict(r) for r in rows]})


# ── Always answer /api/admin/* with JSON, never an HTML error page ──
def _json_errors(e):
    if request.path.startswith('/api/admin'):
        return jsonify({'error': getattr(e, 'description', 'Error')}), e.code
    return e


def register_admin(app, get_db, upload_folder, reports_folder, db_path):
    global _get_db
    _get_db = get_db
    _paths.update(uploads=upload_folder, reports=reports_folder, db=db_path)
    _init_tables()
    app.register_blueprint(bp)
    app.register_error_handler(404, _json_errors)
    app.register_error_handler(405, _json_errors)

    @app.after_request
    def _log_api_call(resp):
        try:
            path = request.path
            if path.startswith('/api/') and not path.startswith('/api/admin'):
                conn = _get_db()
                conn.execute('INSERT INTO api_calls (endpoint, method, status, created_at) VALUES (?,?,?,?)',
                             (path, request.method, resp.status_code, _now()))
                if resp.status_code >= 500:
                    msg = ''
                    if not resp.direct_passthrough:
                        msg = resp.get_data(as_text=True)[:300]
                    conn.execute('INSERT INTO error_logs (endpoint, error_message, user_id, created_at) '
                                 'VALUES (?,?,?,?)',
                                 (path, msg or f'HTTP {resp.status_code}', getattr(__import__("flask").g, 'uid', None), _now()))
                conn.commit()
                conn.close()
        except Exception as e:
            print(f'[admin] call logging failed: {e}')
        return resp