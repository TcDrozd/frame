// Cognito Hosted UI auth: authorization-code flow with PKCE, no libraries.
// Tokens live in sessionStorage; the hosted UI handles first-login password
// change and forgot-password so we never hand-roll those flows.
import { CONFIG } from "./config.js";

const STORE = window.sessionStorage;
const REDIRECT_URI = window.location.origin + "/";

function b64url(bytes) {
  return btoa(String.fromCharCode(...new Uint8Array(bytes)))
    .replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

async function sha256(text) {
  return crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
}

function jwtPayload(token) {
  try {
    return JSON.parse(atob(token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/")));
  } catch {
    return null;
  }
}

function accessTokenValid() {
  const token = STORE.getItem("access_token");
  if (!token) return false;
  const payload = jwtPayload(token);
  // 60s of slack so we refresh before, not after, expiry mid-request.
  return payload && payload.exp * 1000 > Date.now() + 60_000;
}

export function getAccessToken() {
  return STORE.getItem("access_token");
}

async function redirectToLogin() {
  const verifier = b64url(crypto.getRandomValues(new Uint8Array(48)));
  STORE.setItem("pkce_verifier", verifier);
  const challenge = b64url(await sha256(verifier));
  const params = new URLSearchParams({
    response_type: "code",
    client_id: CONFIG.clientId,
    redirect_uri: REDIRECT_URI,
    scope: "openid email",
    code_challenge_method: "S256",
    code_challenge: challenge,
  });
  window.location.assign(`${CONFIG.cognitoDomain}/oauth2/authorize?${params}`);
  // Never resolves; the browser is navigating away.
  return new Promise(() => {});
}

async function tokenRequest(body) {
  const resp = await fetch(`${CONFIG.cognitoDomain}/oauth2/token`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ client_id: CONFIG.clientId, ...body }),
  });
  if (!resp.ok) throw new Error(`token endpoint: HTTP ${resp.status}`);
  return resp.json();
}

function storeTokens(tokens) {
  STORE.setItem("access_token", tokens.access_token);
  if (tokens.id_token) STORE.setItem("id_token", tokens.id_token);
  if (tokens.refresh_token) STORE.setItem("refresh_token", tokens.refresh_token);
}

async function exchangeCode(code) {
  const verifier = STORE.getItem("pkce_verifier");
  if (!verifier) throw new Error("missing PKCE verifier; restarting login");
  const tokens = await tokenRequest({
    grant_type: "authorization_code",
    code,
    redirect_uri: REDIRECT_URI,
    code_verifier: verifier,
  });
  STORE.removeItem("pkce_verifier");
  storeTokens(tokens);
}

export async function tryRefresh() {
  const refreshToken = STORE.getItem("refresh_token");
  if (!refreshToken) return false;
  try {
    storeTokens(await tokenRequest({ grant_type: "refresh_token", refresh_token: refreshToken }));
    return true;
  } catch {
    return false;
  }
}

// Call once at startup. Resolves when a valid access token is in storage,
// otherwise navigates away to the hosted UI.
export async function ensureLogin() {
  const code = new URLSearchParams(window.location.search).get("code");
  if (code) {
    try {
      await exchangeCode(code);
    } catch (err) {
      console.error(err);
      return redirectToLogin();
    }
    // Drop ?code= from the address bar, keep any hash route.
    history.replaceState(null, "", window.location.pathname + window.location.hash);
    return;
  }
  if (accessTokenValid()) return;
  if (await tryRefresh()) return;
  return redirectToLogin();
}

export function logout() {
  STORE.clear();
  const params = new URLSearchParams({ client_id: CONFIG.clientId, logout_uri: REDIRECT_URI });
  window.location.assign(`${CONFIG.cognitoDomain}/logout?${params}`);
}

export function userEmail() {
  const payload = jwtPayload(STORE.getItem("id_token") || "");
  return payload ? payload.email : null;
}
