"""Real loopback HTTP, synthetic WeChat/dot: not a live platform acceptance test."""
import base64
import hashlib
import json
import re
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from cryptography.fernet import Fernet

from wechat_gateway.app import App
from wechat_gateway.auth import Auth, token_hash
from wechat_gateway.rpc import VERSION
from wechat_gateway.store import Binding, EVENT_NAME, Policy, Store, iso
from wechat_gateway.webhooks import Events


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


class RoundtripTests(unittest.TestCase):
    def test_http_auth_event_draft_approval_and_confirmed_reply(self):
        try:
            import uvicorn
        except ImportError:
            self.skipTest("Install requirements-gateway.txt for real HTTP integration.")
        with tempfile.TemporaryDirectory() as folder:
            binding = Binding("owner", "synthetic-account", "synthetic-room")
            policy = Policy(enabled=True, transport_admitted=True, notice_confirmed=True, send_enabled=True)
            store = Store(Path(folder) / "state.sqlite3", (binding,), policy)
            owner, adapter = "test-owner-" + "a" * 32, "test-adapter-" + "b" * 32
            callback = "https://chatgpt.com/connector_platform_oauth_redirect"
            service_tokens = [
                {"role": "owner", "sha256": token_hash(owner), "principal": "owner", "expires": time.time() + 600},
                {"role": "adapter", "sha256": token_hash(adapter), "binding": binding.__dict__, "expires": time.time() + 600}]
            auth = Auth(store, "https://gateway.example", "owner", token_hash(owner), service_tokens, (callback,))
            received = []
            def receiver(url, body, headers):
                payload = json.loads(body)
                if payload.get("type") == "verification":
                    return 200, json.dumps({"challenge": payload["challenge"]}).encode()
                received.append(payload)
                return 202, b"{}"
            app = App(store, Events(store, Fernet.generate_key(), receiver), auth)
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
            sock.listen(16)
            config = uvicorn.Config(app, host="127.0.0.1", port=port, access_log=False, log_level="critical")
            server = uvicorn.Server(config)
            thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
            thread.start()
            deadline = time.monotonic() + 5
            while not server.started and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(server.started)
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

            def request(path, data=None, token=None, extra=None, form=False):
                headers = {"Host": "gateway.example"}
                if token:
                    headers["Authorization"] = "Bearer " + token
                if extra:
                    headers.update(extra)
                if data is None:
                    raw = None
                elif form:
                    raw = urllib.parse.urlencode(data).encode()
                    headers["Content-Type"] = "application/x-www-form-urlencoded"
                else:
                    raw = json.dumps(data).encode()
                    headers["Content-Type"] = "application/json"
                req = urllib.request.Request(f"http://127.0.0.1:{port}" + path, data=raw, headers=headers)
                try:
                    response = opener.open(req, timeout=5)
                except urllib.error.HTTPError as exc:
                    response = exc
                with response:
                    body = response.read()
                    return response.status, response.headers, body

            def json_request(*args, **kwargs):
                status, _, body = request(*args, **kwargs)
                self.assertIn(status, (200, 201))
                return json.loads(body)

            try:
                self.assertEqual(request("/mcp", {})[0], 401)
                client = json_request("/oauth/register", {"redirect_uris": [callback]})["client_id"]
                verifier = "v" * 64
                challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
                params = {"response_type": "code", "client_id": client, "redirect_uri": callback,
                    "scope": "wechat:read wechat:draft wechat:events", "state": "test-state", "resource": auth.resource,
                    "code_challenge_method": "S256", "code_challenge": challenge}
                status, headers, page = request("/oauth/authorize?" + urllib.parse.urlencode(params))
                self.assertEqual(status, 200)
                fields = dict(re.findall(r'name="(request_id|csrf)" value="([^"]+)"', page.decode()))
                status, redirect, _ = request("/oauth/consent", dict(fields, owner_secret=owner), form=True,
                    extra={"Cookie": headers["Set-Cookie"].split(";")[0], "Origin": auth.base_url})
                self.assertEqual(status, 303)
                code = urllib.parse.parse_qs(urllib.parse.urlsplit(redirect["Location"]).query)["code"][0]
                tokens = json_request("/oauth/token", {"grant_type": "authorization_code", "client_id": client,
                    "redirect_uri": callback, "resource": auth.resource, "code": code, "code_verifier": verifier}, form=True)
                dot = tokens["access_token"]

                def rpc(method, params):
                    params = dict(params, _meta={"io.modelcontextprotocol/protocolVersion": VERSION,
                                               "io.modelcontextprotocol/clientCapabilities": {}})
                    headers = {"MCP-Protocol-Version": VERSION, "Mcp-Method": method,
                               "Accept": "application/json, text/event-stream"}
                    if method == "tools/call":
                        headers["Mcp-Name"] = params["name"]
                    return json_request("/mcp", {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, dot, extra=headers)["result"]

                self.assertEqual(rpc("server/discover", {})["supportedVersions"], [VERSION])
                secret = "whsec_" + base64.b64encode(b"s" * 32).decode()
                rpc("events/subscribe", {"name": EVENT_NAME, "arguments": {"account_id": binding.account_id, "room_id": binding.room_id},
                    "delivery": {"mode": "webhook", "url": "https://callback.example/events", "secret": secret}, "cursor": None})
                event = {"schema_version": "0.2", "event_id": "ignored", "occurred_at": iso(time.time()),
                    "channel": "wechat", "account_id": binding.account_id, "room_id": binding.room_id,
                    "sender_id": "synthetic-sender", "message_id": "synthetic-inbound", "message_type": "text",
                    "text": "hi", "mention_bot": True, "from_self": False, "known_bot": False}
                event_id = json_request("/adapter/events", event, adapter)["event_id"]
                deadline = time.monotonic() + 4
                while not received and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertEqual(len(received), 1)
                self.assertEqual(received[0]["eventId"], event_id)
                draft = rpc("tools/call", {"name": "wechat_propose_reply", "arguments": {
                    "event_id": event_id, "text": "received hi", "idempotency_key": "reply-slot-1"}})["structuredContent"]
                self.assertEqual(request("/owner/approve", {"draft_id": draft["draft_id"], "content_hash": draft["content_hash"]}, dot)[0], 403)
                json_request("/owner/approve", {"draft_id": draft["draft_id"], "content_hash": draft["content_hash"]}, owner)
                command = json_request("/adapter/claim", {}, adapter)["command"]
                self.assertEqual(command["room_id"], binding.room_id)
                result = json_request("/adapter/complete", {"draft_id": command["draft_id"], "receipt": {
                    "account_id": binding.account_id, "room_id": binding.room_id, "message_id": "synthetic-outbound",
                    "body_sha256": hashlib.sha256(b"received hi").hexdigest(), "occurred_at": time.time(), "from_self": True}}, adapter)
                self.assertEqual(result["status"], "sent")
                self.assertIsNone(json_request("/adapter/claim", {}, adapter)["command"])
            finally:
                server.should_exit = True
                thread.join(5)
                sock.close()
                self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
