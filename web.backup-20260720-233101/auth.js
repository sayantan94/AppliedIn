// Cognito Hosted-UI auth via Authorization Code + PKCE (public SPA client, no
// secret). Login is currently bypassed in app.js (LOGIN_DISABLED); this module
// stays ready so re-enabling auth is a one-line flip.

const CONFIG = window.APPLIEDIN_CONFIG || {};
const TOKEN_KEY = "appliedin.tokens";

function b64url(bytes) {
  return btoa(String.fromCharCode(...new Uint8Array(bytes)))
    .replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}
async function sha256(str) {
  return b64url(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(str)));
}
function randomVerifier() {
  return b64url(crypto.getRandomValues(new Uint8Array(48)));
}

export const auth = {
  domain: CONFIG.cognitoDomain,
  clientId: CONFIG.clientId,
  redirectUri: CONFIG.redirectUri || location.origin + location.pathname,
  scope: "openid email profile",

  tokens() {
    try { return JSON.parse(sessionStorage.getItem(TOKEN_KEY) || "null"); }
    catch { return null; }
  },
  isValid() {
    const t = this.tokens();
    return !!t && t.expires_at > Date.now() + 5000;
  },
  header() {
    const t = this.tokens();
    return t ? { Authorization: `Bearer ${t.id_token || t.access_token}` } : {};
  },
  async login() {
    const verifier = randomVerifier();
    sessionStorage.setItem("appliedin.pkce", verifier);
    const p = new URLSearchParams({
      response_type: "code",
      client_id: this.clientId,
      redirect_uri: this.redirectUri,
      scope: this.scope,
      code_challenge_method: "S256",
      code_challenge: await sha256(verifier),
    });
    location.assign(`${this.domain}/oauth2/authorize?${p}`);
  },
  async exchange(code) {
    const verifier = sessionStorage.getItem("appliedin.pkce");
    const resp = await fetch(`${this.domain}/oauth2/token`, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({
        grant_type: "authorization_code",
        client_id: this.clientId,
        code,
        redirect_uri: this.redirectUri,
        code_verifier: verifier,
      }),
    });
    if (!resp.ok) throw new Error("token exchange failed");
    const t = await resp.json();
    t.expires_at = Date.now() + (t.expires_in || 3600) * 1000;
    sessionStorage.setItem(TOKEN_KEY, JSON.stringify(t));
    sessionStorage.removeItem("appliedin.pkce");
  },
  logout() {
    sessionStorage.removeItem(TOKEN_KEY);
    if (this.domain) {
      const p = new URLSearchParams({ client_id: this.clientId, logout_uri: this.redirectUri });
      location.assign(`${this.domain}/logout?${p}`);
    } else {
      location.reload();
    }
  },
};
