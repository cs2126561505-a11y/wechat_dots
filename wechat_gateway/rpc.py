"""Stateless MCP 2026-07-28 dispatch; service/owner APIs are not MCP tools."""
from __future__ import annotations

import base64
import json

from jsonschema import Draft202012Validator

from .auth import Identity
from .store import ROOT, GatewayError, canonical, digest

VERSION = "2026-07-28"
SERVER_INFO = {"name": "wechat-dots", "version": "0.1.0"}


class RpcError(GatewayError):
    def __init__(self, code, message, status=400, data=None):
        super().__init__(message)
        self.code, self.status, self.data = code, status, data


def decoded_header(value):
    if value.startswith("=?base64?") and value.endswith("?="):
        try:
            return base64.b64decode(value[9:-2], validate=True).decode("utf-8")
        except (ValueError, UnicodeError):
            raise RpcError(-32020, "Invalid encoded MCP header.") from None
    return value


class RPC:
    def __init__(self, store, events):
        self.store, self.events = store, events
        catalog = json.loads((ROOT / "contracts/tools.json").read_text("utf-8"))
        self.tools = {t["name"]: t for t in catalog["tools"] if t["name"] in catalog["default_exposed_tools"]}

    def check_request(self, request, headers):
        if not isinstance(request, dict) or request.get("jsonrpc") != "2.0" or type(request.get("id")) not in (int, str) or not isinstance(request.get("method"), str):
            raise RpcError(-32600, "Invalid JSON-RPC request.")
        params = request.get("params", {})
        if not isinstance(params, dict):
            raise RpcError(-32602, "Invalid request parameters.")
        meta = params.get("_meta")
        if not isinstance(meta, dict) or not isinstance(meta.get("io.modelcontextprotocol/protocolVersion"), str) or not isinstance(meta.get("io.modelcontextprotocol/clientCapabilities"), dict):
            raise RpcError(-32602, "Required per-request protocol metadata is missing.")
        version = meta["io.modelcontextprotocol/protocolVersion"]
        if headers.get("mcp-protocol-version") != version or headers.get("mcp-method") != request["method"]:
            raise RpcError(-32020, "MCP headers do not match the request body.")
        if version != VERSION:
            raise RpcError(-32022, "Unsupported protocol version.", data={"supportedVersions": [VERSION]})
        if request["method"] == "tools/call" and decoded_header(headers.get("mcp-name", "")) != params.get("name"):
            raise RpcError(-32020, "MCP tool name header does not match.")
        return {k: v for k, v in params.items() if k != "_meta"}

    def dispatch(self, identity: Identity, request, headers):
        request_id = request.get("id") if isinstance(request, dict) else None
        try:
            params = self.check_request(request, headers)
            if identity.role != "dot":
                raise RpcError(-32602, "This credential is not authorized for MCP.", 403)
            result = self.call(identity, request["method"], params)
            result["resultType"] = "complete"
            result["_meta"] = {"io.modelcontextprotocol/serverInfo": SERVER_INFO}
            return 200, {"jsonrpc": "2.0", "id": request_id, "result": result}
        except RpcError as exc:
            error = {"code": exc.code, "message": str(exc)}
            if exc.data is not None:
                error["data"] = exc.data
            return exc.status, {"jsonrpc": "2.0", "id": request_id, "error": error}
        except GatewayError as exc:
            return 400, {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32602, "message": str(exc)}}

    def require(self, identity, scope):
        if scope not in identity.scopes:
            raise RpcError(-32602, "Required OAuth scope is missing.", 403)

    def call(self, identity, method, params):
        if method == "server/discover":
            return {"supportedVersions": [VERSION], "capabilities": {"tools": {}, "events": {}}}
        if method == "ping":
            return {}
        if method == "tools/list":
            if params.get("cursor") is not None or set(params) - {"cursor"}:
                raise RpcError(-32602, "Unsupported catalog cursor.")
            tools = []
            for name, tool in self.tools.items():
                scope = "wechat:draft" if name == "wechat_propose_reply" else "wechat:read"
                if scope in identity.scopes:
                    tools.append(dict(tool, securitySchemes=[{"type": "oauth2", "scopes": [scope]}]))
            return {"tools": sorted(tools, key=lambda t: t["name"])}
        if method == "tools/call":
            if set(params) != {"name", "arguments"} or not isinstance(params["name"], str) or params["name"] not in self.tools:
                raise RpcError(-32602, "Unknown tool or parameters.")
            name, args = params["name"], params["arguments"]
            self.require(identity, "wechat:draft" if name == "wechat_propose_reply" else "wechat:read")
            if not Draft202012Validator(self.tools[name]["inputSchema"]).is_valid(args):
                raise RpcError(-32602, "Invalid tool arguments.")
            try:
                if name == "wechat_get_event":
                    result = self.store.get_event(identity.principal, **args)
                elif name == "wechat_get_context":
                    result = self.store.get_context(identity.principal, **args)
                elif name == "wechat_propose_reply":
                    result = self.store.propose(identity.principal, **args)
                elif name == "wechat_get_connection":
                    result = {"id": digest(identity.principal), "name": "WeChat test gateway",
                              "enabled": self.store.policy.enabled,
                              "sending_enabled": self.store.policy.send_enabled,
                              "rooms": [{"account_id": b.account_id, "room_id": b.room_id}
                                        for b in sorted(self.store.bindings, key=lambda b: (b.account_id, b.room_id))
                                        if b.principal == identity.principal]}
                else:
                    raise GatewayError("Tool is not implemented.")
                return {"structuredContent": result, "content": [{"type": "text", "text": canonical(result)}], "isError": False}
            except GatewayError as exc:
                return {"content": [{"type": "text", "text": str(exc)}], "isError": True}
        if method == "events/list":
            self.require(identity, "wechat:events")
            if params.get("cursor") is not None or set(params) - {"cursor"}:
                raise RpcError(-32602, "Unsupported catalog cursor.")
            return {"events": [self.events.definition()]}
        if method in ("events/subscribe", "events/unsubscribe"):
            self.require(identity, "wechat:events")
            try:
                if method == "events/subscribe":
                    return self.events.subscribe(identity.principal, params, grant_family=identity.grant_family or "")
                return self.events.unsubscribe(identity.principal, params)
            except GatewayError as exc:
                if str(exc).startswith("Callback"):
                    raise RpcError(-32015, "CallbackEndpointError", data={"reason": "challenge_failed"}) from None
                raise
        raise RpcError(-32601, "Method not found.", 404)
