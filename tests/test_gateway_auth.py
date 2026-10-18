import base64
import hashlib
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from wechat_gateway.auth import Auth, SCOPES, token_hash
from wechat_gateway.store import Binding, GatewayError, Policy, Store


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = 1800000000
        self.store = Store(Path(self.tmp.name) / "state.sqlite3", (Binding("owner", "a", "r"),),
                           Policy(), lambda: self.now)
        self.secret = "owner-secret-" + "x" * 32
        self.callback = "https://chatgpt.com/connector_platform_oauth_redirect"
        self.auth = Auth(self.store, "https://gateway.example", "owner", token_hash(self.secret), [], (self.callback,))
        self.client = self.auth.register({"redirect_uris": [self.callback]})["client_id"]
        self.verifier = "v" * 64
        challenge = base64.urlsafe_b64encode(hashlib.sha256(self.verifier.encode()).digest()).decode().rstrip("=")
        self.params = {"response_type": "code", "client_id": self.client, "redirect_uri": self.callback,
                       "resource": self.auth.resource, "code_challenge_method": "S256", "code_challenge": challenge,
                       "scope": " ".join(sorted(SCOPES)), "state": "opaque-client-state"}

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def grant(self):
        pending = self.auth.begin(self.params)
        redirect = self.auth.consent(pending["request_id"], pending["csrf"], self.secret)
        values = parse_qs(urlsplit(redirect).query)
        self.assertEqual(values["state"], ["opaque-client-state"])
        self.assertEqual(values["iss"], [self.auth.base_url])
        return {"grant_type": "authorization_code", "client_id": self.client, "code": values["code"][0],
                "redirect_uri": self.callback, "resource": self.auth.resource, "code_verifier": self.verifier}

    def test_pkce_grant_authentication_and_code_reuse(self):
        params = self.grant()
        with self.assertRaises(GatewayError):
            self.auth.exchange(dict(params, code_verifier="z" * 64))
        result = self.auth.exchange(params)
        identity = self.auth.authenticate("Bearer " + result["access_token"])
        self.assertEqual(identity.principal, "owner")
        self.assertEqual(identity.role, "dot")
        self.assertEqual(identity.scopes, SCOPES)
        with self.assertRaises(GatewayError):
            self.auth.exchange(params)
        with self.assertRaises(GatewayError):
            self.auth.authenticate("Bearer " + result["refresh_token"])

    def test_foreign_redirect_resource_csrf_and_password_rejected(self):
        with self.assertRaises(GatewayError):
            self.auth.register({"redirect_uris": ["https://attacker.example/callback"]})
        for change in ({"resource": "https://other.example/mcp"}, {"code_challenge_method": "plain"}, {"redirect_uri": "https://attacker.example/cb"}):
            with self.assertRaises(GatewayError):
                self.auth.begin(dict(self.params, **change))
        request = self.auth.begin(self.params)
        with self.assertRaises(GatewayError):
            self.auth.consent(request["request_id"], "wrong", self.secret)
        with self.assertRaises(GatewayError):
            self.auth.consent(request["request_id"], request["csrf"], "wrong")

    def test_refresh_rotation_reuse_revokes_family(self):
        tokens = self.auth.exchange(self.grant())
        refresh = {"grant_type": "refresh_token", "client_id": self.client,
                   "refresh_token": tokens["refresh_token"], "resource": self.auth.resource}
        second = self.auth.exchange(refresh)
        family = self.auth.authenticate("Bearer " + second["access_token"]).grant_family
        self.assertTrue(self.auth.has_event_grant("owner", family))
        with self.assertRaises(GatewayError):
            self.auth.exchange(refresh)
        with self.assertRaises(GatewayError):
            self.auth.authenticate("Bearer " + second["access_token"])
        self.assertFalse(self.auth.has_event_grant("owner", family))

    def test_token_expiry_resource_revoke_and_no_cross_issuer_reuse(self):
        params = self.grant()
        with self.assertRaises(GatewayError):
            self.auth.exchange(dict(params, resource="https://other.example/mcp"))
        tokens = self.auth.exchange(params)
        family = self.auth.authenticate("Bearer " + tokens["access_token"]).grant_family
        self.now += 901
        with self.assertRaises(GatewayError):
            self.auth.authenticate("Bearer " + tokens["access_token"])
        self.auth.revoke({"client_id": self.client, "token": tokens["refresh_token"]})
        self.assertFalse(self.auth.has_event_grant("owner", family))
        with self.assertRaises(GatewayError):
            Auth(self.store, "https://other.example", "owner", token_hash(self.secret), [], (self.callback,))

    def test_service_credentials_cannot_become_dot_or_change_binding(self):
        adapter_secret = "adapter-" + "x" * 40
        self.auth.service_tokens = [{"role": "adapter", "sha256": token_hash(adapter_secret),
            "expires": self.now + 300, "binding": {"principal": "owner", "account_id": "a", "room_id": "r"}}]
        identity = self.auth.authenticate("Bearer " + adapter_secret)
        self.assertEqual(identity.role, "adapter")
        self.assertEqual(identity.binding.room_id, "r")
        self.assertEqual(identity.scopes, frozenset())
        self.now += 301
        with self.assertRaises(GatewayError):
            self.auth.authenticate("Bearer " + adapter_secret)


if __name__ == "__main__":
    unittest.main()
