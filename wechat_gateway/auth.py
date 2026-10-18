"""Single-owner OAuth authorization-code/PKCE issuer and separate service tokens.

The owner authenticates in the authorization browser using a locally provisioned
secret. Secrets are never supplied to the model or accepted as MCP arguments.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from urllib.parse import urlencode, urlsplit

from .store import Binding, GatewayError, Store, canonical

SCOPES = frozenset({"wechat:read", "wechat:draft", "wechat:events"})


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass(frozen=True)
class Identity:
    principal: str
    role: str
    scopes: frozenset[str] = frozenset()
    binding: Binding | None = None
    grant_family: str | None = None


class Auth:
    def __init__(self, store: Store, base_url: str, owner_principal: str,
                 owner_secret_hash: str, service_tokens: list[dict], redirects: tuple[str, ...]):
        parsed = urlsplit(base_url)
        if parsed.scheme != "https" or not parsed.netloc or parsed.path or parsed.query or parsed.fragment or parsed.username:
            raise GatewayError("Canonical public HTTPS origin required.")
        if not re.fullmatch(r"[0-9a-f]{64}", owner_secret_hash):
            raise GatewayError("A hashed owner login secret is required.")
        self.store, self.base_url = store, base_url
        self.owner_principal, self.owner_secret_hash = owner_principal, owner_secret_hash
        self.resource = base_url + "/mcp"
        self.service_tokens = service_tokens
        self.redirects = frozenset(redirects)
        for uri in redirects:
            parts = urlsplit(uri)
            if parts.scheme != "https" or not parts.hostname or parts.fragment or parts.username:
                raise GatewayError("Exact HTTPS redirect allowlist required.")
        for item in service_tokens:
            if item.get("role") not in ("owner", "adapter") or not re.fullmatch(r"[0-9a-f]{64}", item.get("sha256", "")):
                raise GatewayError("Invalid service credential configuration.")
            if item["role"] == "adapter" and set(item.get("binding", {})) != {"principal", "account_id", "room_id"}:
                raise GatewayError("Adapter credential must bind one account and room.")
        self.store.db.executescript("""
        CREATE TABLE IF NOT EXISTS oauth_issuer (id INTEGER PRIMARY KEY CHECK(id=1), origin TEXT NOT NULL, principal TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS oauth_clients (
          id TEXT PRIMARY KEY, name TEXT NOT NULL, redirect TEXT NOT NULL, created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS oauth_requests (
          id TEXT PRIMARY KEY, client TEXT NOT NULL, redirect TEXT NOT NULL,
          challenge TEXT NOT NULL, state TEXT NOT NULL, scopes TEXT NOT NULL,
          csrf TEXT NOT NULL, expires REAL NOT NULL, used INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS oauth_codes (
          hash TEXT PRIMARY KEY, client TEXT NOT NULL, redirect TEXT NOT NULL,
          challenge TEXT NOT NULL, principal TEXT NOT NULL, scopes TEXT NOT NULL,
          expires REAL NOT NULL, used INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS oauth_tokens (
          hash TEXT PRIMARY KEY, kind TEXT NOT NULL, family TEXT NOT NULL,
          client TEXT NOT NULL, principal TEXT NOT NULL, scopes TEXT NOT NULL,
          expires REAL NOT NULL, used INTEGER NOT NULL DEFAULT 0, revoked INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS auth_attempts (at REAL NOT NULL, operation TEXT NOT NULL);
        """)
        with self.store.transaction():
            old = self.store.db.execute("SELECT origin,principal FROM oauth_issuer WHERE id=1").fetchone()
            if old is not None and tuple(old) != (base_url, owner_principal):
                raise GatewayError("Issuer or owner changed; use a fresh authorization store.")
            self.store.db.execute("INSERT OR IGNORE INTO oauth_issuer VALUES (1,?,?)", (base_url, owner_principal))

    def resource_metadata(self):
        return {"resource": self.resource, "authorization_servers": [self.base_url],
                "scopes_supported": sorted(SCOPES), "bearer_methods_supported": ["header"]}

    def metadata(self):
        return {"issuer": self.base_url, "authorization_endpoint": self.base_url + "/oauth/authorize",
                "token_endpoint": self.base_url + "/oauth/token",
                "registration_endpoint": self.base_url + "/oauth/register",
                "revocation_endpoint": self.base_url + "/oauth/revoke",
                "response_types_supported": ["code"],
                "grant_types_supported": ["authorization_code", "refresh_token"],
                "code_challenge_methods_supported": ["S256"],
                "token_endpoint_auth_methods_supported": ["none"],
                "authorization_response_iss_parameter_supported": True,
                "scopes_supported": sorted(SCOPES)}

    def limited(self, operation, maximum):
        now = self.store.clock()
        with self.store.transaction():
            self.store.db.execute("DELETE FROM auth_attempts WHERE at<?", (now - 60,))
            count = self.store.db.execute("SELECT COUNT(*) FROM auth_attempts WHERE operation=?", (operation,)).fetchone()[0]
            if count >= maximum:
                raise GatewayError("Too many authorization requests; try again later.")
            self.store.db.execute("INSERT INTO auth_attempts VALUES (?,?)", (now, operation))

    def register(self, params):
        self.limited("register", 10)
        redirects = params.get("redirect_uris")
        if not isinstance(redirects, list) or len(redirects) != 1 or redirects[0] not in self.redirects:
            raise GatewayError("Redirect URI must match the configured ChatGPT callback.")
        if params.get("token_endpoint_auth_method", "none") != "none":
            raise GatewayError("Only public OAuth clients are supported.")
        if params.get("grant_types", ["authorization_code", "refresh_token"]) != ["authorization_code", "refresh_token"]:
            raise GatewayError("Authorization code and refresh grants required.")
        if params.get("response_types", ["code"]) != ["code"]:
            raise GatewayError("Code response required.")
        name = params.get("client_name", "ChatGPT")
        if not isinstance(name, str) or not 1 <= len(name) <= 100:
            raise GatewayError("Invalid client name.")
        client = secrets.token_urlsafe(24)
        with self.store.transaction():
            if self.store.db.execute("SELECT COUNT(*) FROM oauth_clients").fetchone()[0] >= 100:
                raise GatewayError("Client registration capacity reached.")
            self.store.db.execute("INSERT INTO oauth_clients VALUES (?,?,?,?)", (client, name, redirects[0], self.store.clock()))
        return {"client_id": client, "client_id_issued_at": int(self.store.clock()), "redirect_uris": redirects,
                "token_endpoint_auth_method": "none", "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"]}

    def begin(self, params):
        self.limited("authorize", 20)
        if params.get("response_type") != "code" or params.get("resource") != self.resource or params.get("code_challenge_method") != "S256":
            raise GatewayError("Code, resource and S256 PKCE are required.")
        challenge, state = params.get("code_challenge", ""), params.get("state", "")
        if not isinstance(challenge, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", challenge) or not isinstance(state, str) or not 1 <= len(state) <= 1024:
            raise GatewayError("Invalid PKCE challenge or state.")
        scopes = frozenset(params.get("scope", "").split())
        if not scopes or not scopes <= SCOPES:
            raise GatewayError("Unsupported authorization scope.")
        with self.store.transaction():
            client = self.store.db.execute("SELECT * FROM oauth_clients WHERE id=?", (params.get("client_id"),)).fetchone()
            if client is None or client["redirect"] != params.get("redirect_uri") or client["redirect"] not in self.redirects:
                raise GatewayError("Unknown OAuth client or redirect.")
            request, csrf = secrets.token_urlsafe(24), secrets.token_urlsafe(32)
            self.store.db.execute("INSERT INTO oauth_requests VALUES (?,?,?,?,?,?,?,?,0)", (
                request, client["id"], client["redirect"], challenge, state, " ".join(sorted(scopes)), token_hash(csrf), self.store.clock() + 300))
        return {"request_id": request, "csrf": csrf, "client_name": client["name"], "scopes": sorted(scopes)}

    def consent(self, request_id, csrf, owner_secret):
        self.limited("consent", 5)
        if not isinstance(owner_secret, str) or not hmac.compare_digest(token_hash(owner_secret), self.owner_secret_hash):
            raise GatewayError("Owner authentication failed.")
        with self.store.transaction():
            row = self.store.db.execute("SELECT * FROM oauth_requests WHERE id=?", (request_id,)).fetchone()
            if row is None or row["used"] or row["expires"] <= self.store.clock() or not hmac.compare_digest(row["csrf"], token_hash(csrf)):
                raise GatewayError("Authorization request expired or invalid.")
            code = secrets.token_urlsafe(32)
            self.store.db.execute("UPDATE oauth_requests SET used=1 WHERE id=?", (request_id,))
            self.store.db.execute("INSERT INTO oauth_codes VALUES (?,?,?,?,?,?,?,0)", (
                token_hash(code), row["client"], row["redirect"], row["challenge"], self.owner_principal, row["scopes"], self.store.clock() + 120))
        separator = "&" if "?" in row["redirect"] else "?"
        return row["redirect"] + separator + urlencode({"code": code, "state": row["state"], "iss": self.base_url})

    def mint(self, row, family):
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        now = self.store.clock()
        self.store.db.executemany("INSERT INTO oauth_tokens VALUES (?,?,?,?,?,?,?,0,0)", [
            (token_hash(access), "access", family, row["client"], row["principal"], row["scopes"], now + 900),
            (token_hash(refresh), "refresh", family, row["client"], row["principal"], row["scopes"], now + 86400)])
        return {"access_token": access, "refresh_token": refresh, "token_type": "Bearer", "expires_in": 900, "scope": row["scopes"]}

    def exchange(self, params):
        self.limited("token", 30)
        if params.get("resource") != self.resource:
            raise GatewayError("OAuth resource mismatch.")
        if params.get("grant_type") == "authorization_code":
            verifier = params.get("code_verifier", "")
            if not re.fullmatch(r"[A-Za-z0-9._~-]{43,128}", verifier):
                raise GatewayError("Invalid PKCE verifier.")
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
            with self.store.transaction():
                row = self.store.db.execute("SELECT * FROM oauth_codes WHERE hash=?", (token_hash(params.get("code", "")),)).fetchone()
                if (row is None or row["used"] or row["expires"] <= self.store.clock() or row["client"] != params.get("client_id")
                        or row["redirect"] != params.get("redirect_uri") or not hmac.compare_digest(row["challenge"], challenge)):
                    raise GatewayError("Authorization code rejected.")
                self.store.db.execute("UPDATE oauth_codes SET used=1 WHERE hash=?", (row["hash"],))
                return self.mint(row, secrets.token_hex(24))
        if params.get("grant_type") == "refresh_token":
            compromised = False
            with self.store.transaction():
                row = self.store.db.execute("SELECT * FROM oauth_tokens WHERE hash=? AND kind='refresh'", (token_hash(params.get("refresh_token", "")),)).fetchone()
                if row is None or row["client"] != params.get("client_id") or row["revoked"] or row["expires"] <= self.store.clock():
                    raise GatewayError("Refresh token rejected.")
                if "scope" in params and params["scope"] != row["scopes"]:
                    raise GatewayError("Refresh scope change requires new consent.")
                if row["used"]:
                    self.store.db.execute("UPDATE oauth_tokens SET revoked=1 WHERE family=?", (row["family"],))
                    compromised = True
                else:
                    self.store.db.execute("UPDATE oauth_tokens SET used=1 WHERE hash=?", (row["hash"],))
                    result = self.mint(row, row["family"])
            if compromised:
                raise GatewayError("Reused refresh token; authorization revoked.")
            return result
        raise GatewayError("Unsupported OAuth grant.")

    def revoke(self, params):
        self.limited("revoke", 30)
        with self.store.transaction():
            row = self.store.db.execute("SELECT family FROM oauth_tokens WHERE hash=? AND client=?",
                (token_hash(params.get("token", "")), params.get("client_id"))).fetchone()
            if row:
                self.store.db.execute("UPDATE oauth_tokens SET revoked=1 WHERE family=?", (row["family"],))
        return {}

    def authenticate(self, authorization: str) -> Identity:
        if not isinstance(authorization, str) or not authorization.startswith("Bearer "):
            raise GatewayError("Authentication required.")
        token = authorization[7:]
        if not 32 <= len(token) <= 4096:
            raise GatewayError("Authentication required.")
        hashed = token_hash(token)
        for item in self.service_tokens:
            if hmac.compare_digest(item["sha256"], hashed) and item.get("expires", 0) > self.store.clock():
                binding = Binding(**item["binding"]) if item["role"] == "adapter" else None
                return Identity(binding.principal if binding else item["principal"], item["role"], binding=binding)
        with self.store.lock:
            row = self.store.db.execute("SELECT * FROM oauth_tokens WHERE hash=? AND kind='access'", (hashed,)).fetchone()
        if row is None or row["revoked"] or row["expires"] <= self.store.clock():
            raise GatewayError("Authentication required.")
        return Identity(row["principal"], "dot", frozenset(row["scopes"].split()), grant_family=row["family"])

    def has_event_grant(self, principal, family):
        # Unlink/revocation also stops existing webhook subscriptions.
        with self.store.lock:
            rows = self.store.db.execute("SELECT scopes FROM oauth_tokens WHERE principal=? AND family=? AND revoked=0 AND used=0 AND expires>?",
                                         (principal, family, self.store.clock())).fetchall()
        return any("wechat:events" in row[0].split() for row in rows)
