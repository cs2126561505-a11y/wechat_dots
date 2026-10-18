"""Small ASGI surface. Run one worker, bound to loopback behind a TLS proxy."""
from __future__ import annotations

import asyncio
import hmac
import html
import json
import logging
import threading
import time
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, urlsplit

from .auth import Auth
from .rpc import RPC
from .store import GatewayError, canonical


AUDIT_PATHS = frozenset({"/oauth/authorize", "/oauth/consent", "/oauth/register",
                         "/oauth/token", "/oauth/revoke", "/mcp"})
LOGGER = logging.getLogger("uvicorn.error")
AUTH_FAILURES = {
    "Browser authorization check failed.": "browser_cookie_or_csrf",
    "Invalid consent parameters.": "consent_fields",
    "Owner authentication failed.": "owner_credential",
    "Authorization request expired or invalid.": "request_expired_used_or_csrf",
    "Too many authorization requests; try again later.": "rate_limit",
    "Code, resource and S256 PKCE are required.": "authorization_parameters",
    "Unknown OAuth client or redirect.": "client_or_redirect",
    "Unsupported authorization scope.": "scope",
}


def parse_form(raw):
    values = parse_qs(raw.decode("utf-8", errors="strict"), keep_blank_values=True, max_num_fields=20)
    if any(len(v) != 1 for v in values.values()):
        raise GatewayError("Repeated request fields are not accepted.")
    return {k: v[0] for k, v in values.items()}


def parse_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise GatewayError("Repeated JSON fields are not accepted.")
            result[key] = value
        return result

    def invalid(value):
        raise GatewayError("Invalid JSON number.")

    return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid)


class App:
    def __init__(self, store, events, auth: Auth):
        self.store, self.events, self.auth = store, events, auth
        self.rpc = RPC(store, events)
        self.stop = threading.Event()
        self.worker = None
        self.worker_failed = False
        self.events.has_grant = self.auth.has_event_grant

    def background(self):
        last_prune = 0
        try:
            while not self.stop.is_set():
                if time.monotonic() - last_prune > 60:
                    self.store.prune()
                    last_prune = time.monotonic()
                if not self.events.deliver_one():
                    self.stop.wait(1)
        except Exception:
            self.worker_failed = True

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                event = await receive()
                if event["type"] == "lifespan.startup":
                    self.worker = threading.Thread(target=self.background, daemon=True)
                    self.worker.start()
                    await send({"type": "lifespan.startup.complete"})
                elif event["type"] == "lifespan.shutdown":
                    self.stop.set()
                    if self.worker:
                        await asyncio.to_thread(self.worker.join, 15)
                    self.store.close()
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        if scope["type"] != "http":
            return
        original_send = send
        async def send(message):
            if message["type"] == "http.response.start" and scope["path"] in AUDIT_PATHS:
                origin = headers.get("origin")
                origin_kind = ("missing" if origin is None else "opaque" if origin == "null"
                               else "self" if origin == self.auth.base_url
                               else "chatgpt" if origin == "https://chatgpt.com" else "other")
                # Only fixed categories: never URLs with queries, headers, bodies,
                # cookies, authorization codes, secrets, or MCP arguments.
                method = scope["method"] if scope["method"] in ("GET", "POST", "OPTIONS") else "other"
                LOGGER.info("gateway_http path=%s method=%s status=%s origin=%s",
                            scope["path"], method, message["status"], origin_kind)
            await original_send(message)
        headers = {}
        for key, value in scope.get("headers", []):
            name = key.decode("latin1").lower()
            if name in headers:
                await self.respond(send, 400, {"error": "Repeated headers are not accepted."})
                return
            headers[name] = value.decode("latin1")
        origin = headers.get("origin")
        if origin and origin not in (self.auth.base_url, "https://chatgpt.com"):
            await self.respond(send, 403, {"error": "Origin rejected."})
            return
        expected_host = urlsplit(self.auth.base_url).netloc
        if headers.get("host", "").split(":")[0] not in (expected_host, "127.0.0.1", "localhost"):
            await self.respond(send, 403, {"error": "Host rejected."})
            return
        body = bytearray()
        try:
            async with asyncio.timeout(15):
                while True:
                    event = await receive()
                    if event["type"] == "http.disconnect":
                        return
                    body.extend(event.get("body", b""))
                    if len(body) > 262144:
                        await self.respond(send, 413, {"error": "Request too large."})
                        return
                    if not event.get("more_body", False):
                        break
            status, result, extra = await asyncio.to_thread(self.route, scope, headers, bytes(body))
        except GatewayError as exc:
            if scope["path"] in AUDIT_PATHS and scope["path"].startswith("/oauth/"):
                LOGGER.info("gateway_auth_rejected path=%s reason=%s", scope["path"],
                            AUTH_FAILURES.get(str(exc), "invalid_request"))
            status, result, extra = 400, {"error": "Invalid or unauthorized request."}, []
        except (ValueError, UnicodeError, RecursionError):
            status, result, extra = 400, {"error": "Invalid or unauthorized request."}, []
        except TimeoutError:
            status, result, extra = 408, {"error": "Request timed out."}, []
        except Exception:
            status, result, extra = 500, {"error": "Request failed; no automatic send retry."}, []
        await self.respond(send, status, result, extra)

    async def respond(self, send, status, result, extra=()):
        raw = result if isinstance(result, bytes) else canonical(result).encode()
        kind = b"text/html; charset=utf-8" if isinstance(result, bytes) else b"application/json"
        # Chromium also checks form-action at the redirect following consent.
        # Include only configured OAuth callbacks; Auth rejects CSP delimiters.
        callbacks = " ".join(sorted(self.auth.redirects))
        form_action = "'self'" + (" " + callbacks if isinstance(result, bytes) else "")
        csp = f"default-src 'none'; form-action {form_action}; frame-ancestors 'none'; base-uri 'none'"
        headers = [(b"content-type", kind), (b"content-length", str(len(raw)).encode()),
                   (b"cache-control", b"no-store"), (b"x-content-type-options", b"nosniff"),
                   # Fetch's append-Origin algorithm makes a form POST's Origin
                   # opaque under no-referrer, even when posting to this server.
                   # same-origin preserves our consent check and strips Referer
                   # on the cross-origin OAuth callback.
                   (b"referrer-policy", b"same-origin"),
                   (b"content-security-policy", csp.encode("ascii"))]
        headers.extend(extra)
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": raw})

    def route(self, scope, headers, raw):
        path, method = scope["path"], scope["method"]
        if method == "GET" and path == "/health":
            return (503 if self.worker_failed else 200), {"status": "degraded" if self.worker_failed else "ok",
                "wechat_enabled": self.store.policy.enabled, "sending_enabled": self.store.policy.send_enabled}, []
        if method == "GET" and path in ("/.well-known/oauth-protected-resource", "/.well-known/oauth-protected-resource/mcp"):
            return 200, self.auth.resource_metadata(), []
        if method == "GET" and path == "/.well-known/oauth-authorization-server":
            return 200, self.auth.metadata(), []
        if path == "/oauth/authorize" and method == "GET":
            data = self.auth.begin(parse_form(scope.get("query_string", b"")))
            client = html.escape(data["client_name"])
            request_id, csrf = data["request_id"], data["csrf"]
            page = f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>连接微信测试网关</title>
            <h1>连接微信测试网关</h1><p>应用：{client}</p>
            <p>允许读取已授权的测试群事件、订阅新事件并创建回复草稿。发送仍需所有者独立审核。</p>
            <form method="post" action="/oauth/consent">
            <input type="hidden" name="request_id" value="{request_id}">
            <input type="hidden" name="csrf" value="{csrf}">
            <label>本地保存的网关授权口令 <input name="owner_secret" type="password" autocomplete="current-password" required></label>
            <button type="submit">授权连接</button></form></html>'''
            return 200, page.encode(), [(b"set-cookie", f"__Host-wx-oauth={csrf}; Path=/; Secure; HttpOnly; SameSite=Lax; Max-Age=300".encode())]
        if path.startswith("/oauth/") and method == "POST":
            content_type = headers.get("content-type", "").split(";")[0]
            if path == "/oauth/register" and content_type == "application/json":
                params = parse_json(raw)
                if not isinstance(params, dict):
                    raise GatewayError("Object required.")
                return 201, self.auth.register(params), []
            if content_type != "application/x-www-form-urlencoded":
                raise GatewayError("Form request required.")
            params = parse_form(raw)
            if path == "/oauth/consent":
                cookies = SimpleCookie(headers.get("cookie", ""))
                saved = cookies.get("__Host-wx-oauth")
                if headers.get("origin") != self.auth.base_url or saved is None or not hmac.compare_digest(saved.value, params.get("csrf", "")):
                    raise GatewayError("Browser authorization check failed.")
                if set(params) != {"request_id", "csrf", "owner_secret"}:
                    raise GatewayError("Invalid consent parameters.")
                location = self.auth.consent(**params)
                return 303, {}, [(b"location", location.encode()), (b"set-cookie", b"__Host-wx-oauth=; Path=/; Secure; HttpOnly; SameSite=Lax; Max-Age=0")]
            if path == "/oauth/token":
                try:
                    return 200, self.auth.exchange(params), []
                except GatewayError:
                    return 400, {"error": "invalid_grant"}, []
            if path == "/oauth/revoke":
                return 200, self.auth.revoke(params), []
        if path == "/mcp" and method != "POST":
            return 405, {"error": "Use POST."}, [(b"allow", b"POST")]
        try:
            identity = self.auth.authenticate(headers.get("authorization", ""))
        except GatewayError:
            challenge = f'Bearer resource_metadata="{self.auth.base_url}/.well-known/oauth-protected-resource/mcp"'
            return 401, {"error": "Authentication required."}, [(b"www-authenticate", challenge.encode())]
        if method == "POST" and headers.get("content-type", "").split(";")[0] != "application/json":
            return 415, {"error": "JSON required."}, []
        data = parse_json(raw) if raw else {}
        if path == "/mcp":
            accept = headers.get("accept", "")
            if "application/json" not in accept or "text/event-stream" not in accept:
                return 406, {"error": "MCP JSON and event-stream Accept types required."}, []
            status, result = self.rpc.dispatch(identity, data, headers)
            return status, result, []
        if not isinstance(data, dict):
            raise GatewayError("Object required.")
        if identity.role == "adapter" and method == "POST":
            if path == "/adapter/events":
                return 200, self.store.ingest(identity.binding, data), []
            if path == "/adapter/claim" and not data:
                self.store.authorize(identity.binding)
                with self.store.lock:
                    row = self.store.db.execute("""SELECT id FROM drafts WHERE principal=? AND account=? AND room=?
                        AND status='approved' AND expires>? ORDER BY created LIMIT 1""", (
                        identity.principal, identity.binding.account_id, identity.binding.room_id, self.store.clock())).fetchone()
                return 200, {"command": self.store.claim_send(identity.binding, row[0]) if row else None}, []
            if path == "/adapter/complete" and set(data) == {"draft_id", "receipt"}:
                return 200, self.store.finish_send(identity.binding, **data), []
        if identity.role == "owner":
            if path == "/owner/review" and method == "POST" and set(data) == {"draft_id"}:
                return 200, self.store.review(identity.principal, **data), []
            if path == "/owner/approve" and method == "POST" and set(data) == {"draft_id", "content_hash"}:
                return 200, self.store.approve(identity.principal, owner=identity.principal, **data), []
        return 403, {"error": "Operation is outside the credential's scope."}, []
