import asyncio
import json
import re
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlencode

from cryptography.fernet import Fernet

from wechat_gateway.app import App
from wechat_gateway.auth import Auth, Identity, SCOPES, token_hash
from wechat_gateway.rpc import VERSION
from wechat_gateway.store import Binding, Policy, Store
from wechat_gateway.webhooks import Events


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "state.sqlite3", (Binding("owner", "a", "r"),), Policy())
        self.auth = Auth(self.store, "https://gateway.example", "owner", token_hash("x" * 32), [],
                         ("https://chatgpt.com/connector_platform_oauth_redirect",))
        self.app = App(self.store, Events(self.store, Fernet.generate_key()), self.auth)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def request(self, path="/mcp", body=b"{}", headers=None, method="POST", query=b"", raw=False):
        response = []
        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}
        async def send(message):
            response.append(message)
        scope = {"type": "http", "method": method, "path": path, "query_string": query,
                 "headers": [(k.encode(), v.encode()) for k, v in (headers or {"host": "gateway.example"}).items()]}
        asyncio.run(self.app(scope, receive, send))
        if raw:
            return response[0], response[1]["body"]
        return response[0]["status"], json.loads(response[1]["body"])

    def test_browser_consent_policy_csrf_and_origin(self):
        callback = "https://chatgpt.com/connector_platform_oauth_redirect"
        client = self.auth.register({"redirect_uris": [callback]})["client_id"]
        query = urlencode({"response_type": "code", "resource": self.auth.resource,
            "client_id": client, "redirect_uri": callback, "state": "test-state",
            "scope": "wechat:read", "code_challenge_method": "S256", "code_challenge": "a" * 43}).encode()
        response, body = self.request("/oauth/authorize", method="GET", query=query, raw=True)
        headers = dict(response["headers"])
        self.assertEqual(response["status"], 200)
        self.assertEqual(headers[b"referrer-policy"], b"same-origin")
        self.assertIn(b"form-action 'self' " + callback.encode() + b";", headers[b"content-security-policy"])
        self.assertNotIn(b"*", headers[b"content-security-policy"])
        fields = dict(re.findall(r'name="(request_id|csrf)" value="([^"]+)"', body.decode()))
        form = urlencode(dict(fields, owner_secret="x" * 32)).encode()
        request_headers = {"host": "gateway.example", "content-type": "application/x-www-form-urlencoded",
            "cookie": headers[b"set-cookie"].decode().split(";")[0], "origin": self.auth.base_url}
        # Fix the browser policy instead of allowing opaque or foreign origins.
        for origin in ("null", "https://attacker.example"):
            self.assertEqual(self.request("/oauth/consent", form, dict(request_headers, origin=origin))[0], 403)
        with self.assertLogs("uvicorn.error", level="INFO") as captured:
            self.assertEqual(self.request("/oauth/consent", form, dict(request_headers, cookie=""))[0], 400)
        self.assertIn("reason=browser_cookie_or_csrf", " ".join(captured.output))
        self.assertNotIn(fields["csrf"], " ".join(captured.output))
        wrong_csrf = urlencode(dict(fields, csrf="mismatch", owner_secret="x" * 32)).encode()
        self.assertEqual(self.request("/oauth/consent", wrong_csrf, request_headers)[0], 400)
        with self.assertLogs("uvicorn.error", level="INFO") as captured:
            bad_password = urlencode(dict(fields, owner_secret="wrong-password")).encode()
            self.assertEqual(self.request("/oauth/consent", bad_password, request_headers)[0], 400)
        self.assertIn("reason=owner_credential", " ".join(captured.output))
        self.assertNotIn("wrong-password", " ".join(captured.output))
        response, _ = self.request("/oauth/consent", form, request_headers, raw=True)
        self.assertEqual(response["status"], 303)
        self.assertTrue(dict(response["headers"])[b"location"].startswith(callback.encode() + b"?"))

    def test_http_audit_uses_only_fixed_categories(self):
        with self.assertLogs("uvicorn.error", level="INFO") as captured:
            self.request("/oauth/authorize", method="GET", query=b"secret-query=never-log-me",
                         headers={"host": "gateway.example", "origin": "https://private.example"},
                         body=b"secret-body=never-log-me")
        message = " ".join(captured.output)
        self.assertIn("path=/oauth/authorize method=GET status=403 origin=other", message)
        self.assertNotIn("private.example", message)
        self.assertNotIn("never-log-me", message)

    def rpc(self, method, params=None, headers=None, identity=None):
        args = dict(params or {})
        args["_meta"] = {"io.modelcontextprotocol/protocolVersion": VERSION, "io.modelcontextprotocol/clientCapabilities": {}}
        request = {"jsonrpc": "2.0", "id": 1, "method": method, "params": args}
        hdr = {"mcp-protocol-version": VERSION, "mcp-method": method}
        if "name" in args:
            hdr["mcp-name"] = args["name"]
        if headers:
            hdr.update(headers)
        return self.app.rpc.dispatch(identity or Identity("owner", "dot", SCOPES), request, hdr)

    def test_missing_auth_and_origin_body_limit(self):
        self.assertEqual(self.request()[0], 401)
        self.assertEqual(self.request(headers={"host": "gateway.example", "origin": "https://attacker.example"})[0], 403)
        self.assertEqual(self.request(body=b"x" * 262145)[0], 413)
        self.assertEqual(self.request(path="/.well-known/oauth-protected-resource/mcp", method="GET")[1]["resource"], "https://gateway.example/mcp")

    def test_discovery_header_matching_and_no_send_tool(self):
        status, result = self.rpc("server/discover")
        self.assertEqual(status, 200)
        self.assertEqual(result["result"]["supportedVersions"], [VERSION])
        status, result = self.rpc("tools/list")
        self.assertNotIn("wechat_send_reply", {t["name"] for t in result["result"]["tools"]})
        self.assertEqual(self.rpc("tools/list", headers={"mcp-method": "tools/call"})[1]["error"]["code"], -32020)
        self.assertEqual(self.rpc("not-a-method")[0], 404)

    def test_dot_cannot_approve_send_or_supply_destination(self):
        for name, arguments in (("wechat_send_reply", {"draft_id": "d"}),
                                ("wechat_propose_reply", {"event_id": "e", "text": "hi", "idempotency_key": "k", "approved": True})):
            status, _ = self.rpc("tools/call", {"name": name, "arguments": arguments})
            self.assertEqual(status, 400)
        self.assertEqual(self.rpc("tools/list", identity=Identity("owner", "adapter"))[0], 403)
        status, response = self.rpc("tools/list", identity=Identity("owner", "dot", frozenset({"wechat:read"})))
        self.assertNotIn("wechat_propose_reply", {t["name"] for t in response["result"]["tools"]})


if __name__ == "__main__":
    unittest.main()
