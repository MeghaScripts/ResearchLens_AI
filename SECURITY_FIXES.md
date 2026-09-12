# Security fixes — ResearchLens AI

This covers the six "fix this week" items. Five are done in code below.
The sixth (revoke the leaked key + scrub git history) only you can do,
since it requires your Groq account and your GitHub repo — exact
commands are at the bottom.

## Files changed

| File | What changed |
|---|---|
| `backend/auth.py` | **New.** Verifies Firebase ID tokens server-side (`@require_auth` decorator). |
| `backend/crypto_utils.py` | **New.** Encrypts/decrypts secrets (Groq keys) before they touch the DB. |
| `backend/app.py` | `@require_auth` added to all 30 `/api/*` routes. Path traversal fixed on `/api/download`. Ownership (`owner_uid`) enforced on analyses, watched topics, and generated report files. Groq keys encrypted before being stored for watched topics. |
| `frontend/login.html` | Replaced the custom OTP-in-Firestore + auto-generated-password-in-Firestore flow with Firebase's built-in passwordless email-link sign-in. No more `otps` or `userPasswords` writes. |
| `frontend/static/js/firebase-auth.js` | **New.** Shared auth bootstrap: page guard (`requireAuth()`) + patches `window.fetch` so every existing `/api/...` call in `app.js` automatically carries a real `Authorization: Bearer <idToken>` header. |
| `frontend/index.html` | Loads `firebase-auth.js` before `app.js`, calls `requireAuth()` — unauthenticated visitors are redirected to `/login` instead of seeing the full app. |
| `frontend/static/js/app.js` | Nav user-info and logout now driven by real Firebase auth state instead of only a localStorage display cache. |
| `firestore.rules` | **New.** Default-deny; only `users/{uid}` is readable/writable, and only by that uid. |
| `requirements.txt` | Added `firebase-admin`, `cryptography`. |

## What each backend item required, and why

**1. Backend auth enforcement.** Previously every `/api/*` route trusted
whatever the client sent — the login page never told Flask anything.
`backend/auth.py` verifies a real Firebase ID token on every request via
`firebase_admin.auth.verify_id_token()`. Bad, missing, or expired tokens
get a 401 before the route body ever runs (tested — see below).

**2. Path traversal on `/api/download/<filename>`.** Fixed with
`werkzeug.secure_filename`, a `commonpath` check that the resolved path
stays inside `REPORTS_FOLDER`, and (see #3) an ownership check on top.

**3. Ownership checks.** Added a `generated_files` table mapping every
report/bibtex file to the Firebase uid that generated it, and an
`owner_uid` column on `analyses` and `watched_topics`, checked on every
read/update/delete.

**4. Firestore rules for `otps`/`userPasswords`.** Went with your second
option — dropped the custom flow entirely rather than trying to lock down
rules around it. `login.html` now uses Firebase's own passwordless
email-link sign-in, so there's nothing sensitive being written to
Firestore from the client anymore. `firestore.rules` is default-deny
except for `users/{uid}`, owned by that uid.

**5. Plaintext Groq keys in the DB.** `watched_topics.api_key` is now
encrypted with Fernet before being written, decrypted only in-process
when the scheduler needs to call Groq. Requires an `APP_SECRET_KEY` env
var (below) — the app refuses to start without it rather than silently
falling back to something insecure.

## New required environment variables

```
APP_SECRET_KEY=<a long random string, e.g. `python3 -c "import secrets;print(secrets.token_urlsafe(32))"`>
FIREBASE_SERVICE_ACCOUNT_JSON=<the full JSON contents of a Firebase service-account key, as one env var>
```

Get the service account JSON from Firebase console → Project settings →
Service accounts → Generate new private key. **Do not commit this file.**
On Render, paste its contents as the value of `FIREBASE_SERVICE_ACCOUNT_JSON`
in the dashboard's environment variables section. Locally, you can instead
set `GOOGLE_APPLICATION_CREDENTIALS=/path/to/the/file.json`.

Deploy the Firestore rules once, from a machine with the Firebase CLI:
```
firebase deploy --only firestore:rules
```

## Verified locally

Ran the updated app with `flask.test_client()` and a throwaway RSA test
key (not your real credentials):

```
no-auth /api/watched-topics: 401 {'error': 'Missing or malformed Authorization header'}
no-auth /api/analyze:        401 {'error': 'Missing or malformed Authorization header'}
bad-token /api/download/...: 401 {'error': 'Invalid or expired auth token'}
../../etc/passwd traversal:  404 (blocked by Werkzeug routing + secure_filename, never touches disk)
crypto_utils roundtrip:      plaintext Groq key never appears in what's stored
```

## What you still need to do yourself

### 1. Revoke the leaked Groq key
Go to the [Groq console](https://console.groq.com/keys) → find the
exposed key → **revoke/delete it**. Issue a new one and only ever put it
in environment variables or have users supply their own at request time
(which the app already does for on-demand analysis — it's only the
*watched-topics* background feature that has to persist one, and that's
now encrypted at rest).

### 2. Scrub git history
If that key (or anything else sensitive) was ever committed, revoking it
is necessary but rewriting history is what actually removes it from the
repo. From a fresh clone:

```bash
pip install git-filter-repo   # or: brew install git-filter-repo
git clone --mirror https://github.com/YOUR_USERNAME/researchlens-ai.git
cd researchlens-ai.git
git filter-repo --replace-text <(echo 'gsk_YOUR_LEAKED_KEY_HERE==>REDACTED')
git push --force --all
git push --force --tags
```
Then have every collaborator re-clone (don't just `git pull` — old
history is still in their local repo/reflog too). GitHub also caches old
commits in its own systems for a period; consider it burned regardless
and rely on step 1 (revocation) as the real fix, not history rewriting
alone.

### 3. Check for other leaked secrets
While you're in there:
```bash
git log -p | grep -iE "api[_-]?key|secret|password|firebase.*private_key" 
```
Rotate anything that turns up, the same way.

### 4. Clean up old Firestore data
If the `otps` or `userPasswords` collections still have data in them from
before this fix, delete those collections from the Firebase console.
`firestore.rules` now blocks all client access to them, but the data
still exists server-side/in backups until you delete it.

## Known follow-up (not urgent, noted for completeness)

`watched_topics.topic` still has a table-level `UNIQUE` constraint from
before per-user ownership existed, so two different users can't watch
the same topic string — the newer request is now rejected with a 409
instead of silently overwriting someone else's watch (a correctness fix,
not a security one), but the real fix is a small SQLite migration to a
composite `(topic, owner_uid)` uniqueness. Also flagged: `admin.html`
calls `/api/admin/*` routes that don't exist anywhere in `app.py` — it's
currently inert (every call 404s), but worth deleting or actually wiring
up with its own auth if it's meant to ship.
