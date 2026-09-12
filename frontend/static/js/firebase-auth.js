/*
 * ResearchLens AI — shared Firebase auth bootstrap.
 *
 * Before this file existed, "login" was 100% cosmetic: login.html signed
 * the user into Firebase client-side, wrote a display profile to
 * Firestore, and stashed a name in localStorage — but never told the
 * backend anything. app.js's `fetch('/api/...')` calls carried no proof
 * of identity at all, so the Flask API had to trust the request body,
 * and index.html never even checked whether anyone was signed in before
 * rendering the full app.
 *
 * This file is the single source of truth for auth state on every page.
 * Include it BEFORE app.js / any other script that calls the API.
 */
import { initializeApp, getApps } from "https://www.gstatic.com/firebasejs/10.12.0/firebase-app.js";
import { getAuth, onAuthStateChanged, signOut } from "https://www.gstatic.com/firebasejs/10.12.0/firebase-auth.js";

// This is Firebase's public *web API key* — unlike a Groq/API secret key,
// it is designed to be embedded in client code and is not sufficient on
// its own to read or write data; that's what Firestore security rules
// (see firestore.rules) and this app's own backend auth checks are for.
const firebaseConfig = {
  apiKey: "AIzaSyA-AqkSLbkLgyyHZiQyxyKHrF7Xj5FIK-s",
  authDomain: "researchlens-ai-3c90b.firebaseapp.com",
  projectId: "researchlens-ai-3c90b",
  storageBucket: "researchlens-ai-3c90b.firebasestorage.app",
  messagingSenderId: "725178001793",
  appId: "1:725178001793:web:52a96bbc7422ef19f4e080"
};

const app = getApps().length ? getApps()[0] : initializeApp(firebaseConfig);
const auth = getAuth(app);

let currentUser = null;
let readyResolve;
const ready = new Promise(res => { readyResolve = res; });

onAuthStateChanged(auth, (user) => {
  currentUser = user;
  readyResolve();
  window.dispatchEvent(new CustomEvent('rl-auth-changed', { detail: { user } }));
});

/** Resolves once Firebase has restored (or confirmed the absence of) a session. */
async function authReady() {
  await ready;
  return currentUser;
}

/** Current signed-in Firebase user, or null. Only meaningful after authReady(). */
function getCurrentUser() {
  return currentUser;
}

/** Fresh ID token for the signed-in user, or null if signed out. */
async function getIdToken() {
  const user = await authReady();
  if (!user) return null;
  return user.getIdToken();
}

/**
 * Call at the top of any page that requires a signed-in user
 * (index.html, etc). Redirects to /login if nobody is signed in.
 */
async function requireAuth() {
  const user = await authReady();
  if (!user) {
    window.location.href = '/login';
    return null;
  }
  return user;
}

function logout() {
  localStorage.removeItem('rl_user');
  return signOut(auth).then(() => { window.location.href = '/'; });
}

// ── Patch window.fetch so every existing `fetch('/api/...')` call in
//    app.js automatically carries a verified Authorization header. This
//    is deliberately done here, once, instead of hand-editing every call
//    site in app.js, so nothing can accidentally ship un-authenticated. ──
const _nativeFetch = window.fetch.bind(window);
window.fetch = async function (input, init) {
  const url = typeof input === 'string' ? input : (input && input.url) || '';
  if (url.startsWith('/api/')) {
    const token = await getIdToken();
    init = init || {};
    init.headers = new Headers(init.headers || {});
    if (token) {
      init.headers.set('Authorization', 'Bearer ' + token);
    }
    return _nativeFetch(input, init);
  }
  return _nativeFetch(input, init);
};

window.rlAuth = { authReady, getCurrentUser, getIdToken, requireAuth, logout, auth };
