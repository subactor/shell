from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

from . import __version__
from .chat import ChatError, ChatService
from .orchestration import OrchestrationError


class AcpProtocolError(RuntimeError):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class AcpAgent:
    """Minimal ACP v1 stdio agent backed by the same persistent ChatService."""

    def __init__(self, chat: ChatService):
        self.chat = chat
        self.initialized = False
        self._write_lock = asyncio.Lock()
        self._active_turns: dict[str, asyncio.Event] = {}
        self._tasks: set[asyncio.Task[Any]] = set()

    async def _write(self, message: dict[str, Any]) -> None:
        encoded = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        async with self._write_lock:
            sys.stdout.write(encoded + "\n")
            sys.stdout.flush()

    async def _response(self, request_id: Any, result: Any) -> None:
        await self._write({"jsonrpc": "2.0", "id": request_id, "result": result})

    async def _error(self, request_id: Any, code: int, message: str) -> None:
        await self._write(
            {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
        )

    async def _notify_update(self, session_id: str, update: dict[str, Any]) -> None:
        await self._write(
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {"sessionId": session_id, "update": update},
            }
        )

    async def run_stdio(self) -> None:
        while True:
            line = await asyncio.to_thread(sys.stdin.buffer.readline)
            if not line:
                break
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                await self._error(None, -32700, "Parse error")
                continue
            if not isinstance(message, dict):
                await self._error(None, -32600, "Invalid Request")
                continue
            method = message.get("method")
            if method == "session/cancel" and "id" not in message:
                params = message.get("params", {})
                session_id = params.get("sessionId") if isinstance(params, dict) else None
                event = self._active_turns.get(str(session_id))
                if event:
                    event.set()
                continue
            task = asyncio.create_task(self._handle(message))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _handle(self, message: dict[str, Any]) -> None:
        request_id = message.get("id")
        method = message.get("method")
        if not isinstance(method, str):
            if "id" in message:
                await self._error(request_id, -32600, "Invalid Request")
            return
        try:
            result = await self._dispatch(method, message.get("params", {}))
            if "id" in message:
                await self._response(request_id, result)
        except AcpProtocolError as exc:
            if "id" in message:
                await self._error(request_id, exc.code, exc.message)
        except (ChatError, OrchestrationError, KeyError, ValueError) as exc:
            if "id" in message:
                await self._error(request_id, -32000, str(exc))
        except Exception as exc:  # pragma: no cover - defensive protocol boundary
            print(f"subactor-shell ACP error: {type(exc).__name__}", file=sys.stderr)
            if "id" in message:
                await self._error(request_id, -32603, "Internal error")

    async def _dispatch(self, method: str, params: Any) -> Any:
        if not isinstance(params, dict):
            raise AcpProtocolError(-32602, "Invalid params")
        if method == "initialize":
            requested = params.get("protocolVersion")
            if not isinstance(requested, int):
                raise AcpProtocolError(-32602, "protocolVersion is required")
            self.initialized = True
            return {
                "protocolVersion": 1,
                "agentCapabilities": {
                    "loadSession": True,
                    "promptCapabilities": {"embeddedContext": True},
                    "_meta": {
                        "com.subactor.secretReferences": True,
                        "com.subactor.persistentData": True,
                        "com.subactor.intentIR": "v1",
                        "com.subactor.tokenBudget": True,
                        "com.subactor.executionPlans": True,
                        "com.subactor.executionReceipts": True,
                        "com.subactor.namedConnectors": True,
                    },
                },
                "agentInfo": {
                    "name": "subactor-shell",
                    "title": "Subactor Shell Bridge",
                    "version": __version__,
                },
                "authMethods": [],
            }
        if not self.initialized:
            raise AcpProtocolError(-32002, "Connection not initialized")
        if method == "session/new":
            cwd = str(params.get("cwd", ""))
            name = f"ACP: {Path(cwd).name}" if cwd else "ACP conversation"
            session = self.chat.new_session(name=name)
            return {"sessionId": session.id}
        if method == "session/load":
            session_id = self._session_id(params)
            if not self.chat.store.get_session(session_id):
                raise AcpProtocolError(-32001, f"Unknown session: {session_id}")
            for message in self.chat.store.list_messages(session_id):
                kind = "user_message_chunk" if message.role == "user" else "agent_message_chunk"
                if message.role == "system":
                    continue
                await self._notify_update(
                    session_id,
                    {
                        "sessionUpdate": kind,
                        "messageId": f"msg_{message.id}",
                        "content": {"type": "text", "text": message.display_content},
                    },
                )
            return None
        if method == "session/prompt":
            return await self._prompt(params)
        if method == "session/cancel":
            session_id = self._session_id(params)
            event = self._active_turns.get(session_id)
            if event:
                event.set()
            return None
        if method == "subactor/secret/bind":
            alias = str(params.get("alias", ""))
            reference = str(params.get("reference", ""))
            self.chat.bind_secret(alias, reference)
            return {}
        if method == "subactor/secret/grant":
            alias = str(params.get("alias", ""))
            self.chat.grant_secret(alias)
            return {"granted": True, "oneTime": True}
        if method == "subactor/data/set":
            name = str(params.get("name", ""))
            value = str(params.get("value", ""))
            self.chat.set_data_text(name, value)
            return {}
        if method == "subactor/data/list":
            return {
                "items": [
                    {
                        "name": name,
                        "kind": kind,
                        "value": value if kind == "artifact" else f"{len(value)} chars",
                    }
                    for name, kind, value in self.chat.store.list_data()
                ]
            }
        if method == "subactor/secret/list":
            return {
                "bindings": [
                    {"alias": alias, "reference": reference}
                    for alias, reference in self.chat.store.list_secret_bindings()
                ]
            }
        if method == "subactor/plan/list":
            session_id = params.get("sessionId")
            if session_id is not None and not isinstance(session_id, str):
                raise AcpProtocolError(-32602, "sessionId must be a string")
            limit = self._limit(params)
            return {
                "plans": self.chat.store.list_execution_plans(session_id or None, limit=limit)
            }
        if method == "subactor/plan/get":
            plan_id = self._required_string(params, "planId")
            plan = self.chat.store.get_execution_plan(plan_id)
            if not plan:
                raise AcpProtocolError(-32001, f"Unknown plan: {plan_id}")
            return {"plan": plan}
        if method == "subactor/plan/apply":
            plan_id = self._required_string(params, "planId")
            confirmation = str(params.get("confirmation", ""))
            receipt = await self.chat.orchestration.apply_plan(
                plan_id, confirmation=confirmation
            )
            return {"receipt": receipt.to_dict()}
        if method == "subactor/receipt/list":
            session_id = params.get("sessionId")
            if session_id is not None and not isinstance(session_id, str):
                raise AcpProtocolError(-32602, "sessionId must be a string")
            limit = self._limit(params)
            return {
                "receipts": self.chat.store.list_execution_receipts(
                    session_id or None, limit=limit
                )
            }
        if method == "subactor/receipt/get":
            receipt_id = self._required_string(params, "receiptId")
            receipt = self.chat.store.get_execution_receipt(receipt_id)
            if not receipt:
                raise AcpProtocolError(-32001, f"Unknown receipt: {receipt_id}")
            return {"receipt": receipt}
        if method == "subactor/metrics/get":
            session_id = params.get("sessionId")
            if session_id is not None and not isinstance(session_id, str):
                raise AcpProtocolError(-32602, "sessionId must be a string")
            return {"metrics": self.chat.store.usage_summary(session_id or None)}
        if method == "subactor/catalog/list":
            return {
                "fingerprint": self.chat.orchestration.catalog.fingerprint,
                "intents": [
                    item.to_summary() for item in self.chat.orchestration.catalog.list()
                ],
            }
        if method == "subactor/connectors/list":
            return {
                "fingerprint": self.chat.orchestration.registry.fingerprint,
                "connectors": [
                    item.public_dict() for item in self.chat.orchestration.registry.list()
                ],
            }
        if method == "subactor/route/get":
            session_id = self._session_id(params)
            return {
                "route": self.chat.store.last_routing_decision(session_id),
                "workingState": self.chat.store.get_session_state(session_id),
            }
        raise AcpProtocolError(-32601, "Method not found")

    @staticmethod
    def _session_id(params: dict[str, Any]) -> str:
        session_id = params.get("sessionId")
        if not isinstance(session_id, str) or not session_id:
            raise AcpProtocolError(-32602, "sessionId is required")
        return session_id

    @staticmethod
    def _required_string(params: dict[str, Any], name: str) -> str:
        value = params.get(name)
        if not isinstance(value, str) or not value:
            raise AcpProtocolError(-32602, f"{name} is required")
        return value

    @staticmethod
    def _limit(params: dict[str, Any], default: int = 100) -> int:
        value = params.get("limit", default)
        if isinstance(value, bool) or not isinstance(value, int):
            raise AcpProtocolError(-32602, "limit must be an integer")
        if value < 1 or value > 500:
            raise AcpProtocolError(-32602, "limit must be between 1 and 500")
        return value

    async def _prompt(self, params: dict[str, Any]) -> dict[str, str]:
        session_id = self._session_id(params)
        if session_id in self._active_turns:
            raise AcpProtocolError(-32003, "A prompt is already active for this session")
        prompt = params.get("prompt")
        if not isinstance(prompt, list):
            raise AcpProtocolError(-32602, "prompt must be a ContentBlock array")
        text, context_blocks = self._content_to_text(prompt)
        cancel_event = asyncio.Event()
        self._active_turns[session_id] = cancel_event
        message_id = "msg_agent_" + uuid.uuid4().hex
        try:
            async for chunk in self.chat.stream_message(
                session_id,
                text,
                additional_context_blocks=context_blocks,
                cancel_event=cancel_event,
            ):
                await self._notify_update(
                    session_id,
                    {
                        "sessionUpdate": "agent_message_chunk",
                        "messageId": message_id,
                        "content": {"type": "text", "text": chunk},
                    },
                )
            return {"stopReason": "cancelled" if cancel_event.is_set() else "end_turn"}
        finally:
            self._active_turns.pop(session_id, None)

    @staticmethod
    def _content_to_text(blocks: list[Any]) -> tuple[str, list[str]]:
        text_parts: list[str] = []
        context_blocks: list[str] = []
        for block in blocks:
            if not isinstance(block, dict):
                raise AcpProtocolError(-32602, "Invalid content block")
            kind = block.get("type")
            if kind == "text":
                value = block.get("text")
                if not isinstance(value, str):
                    raise AcpProtocolError(-32602, "Text block requires text")
                text_parts.append(value)
            elif kind == "resource":
                resource = block.get("resource")
                if not isinstance(resource, dict):
                    raise AcpProtocolError(-32602, "Resource block requires resource")
                uri = str(resource.get("uri", "embedded://resource"))
                mime = str(resource.get("mimeType", "application/octet-stream"))
                if isinstance(resource.get("text"), str):
                    content = resource["text"]
                elif isinstance(resource.get("blob"), str):
                    try:
                        raw = base64.b64decode(resource["blob"], validate=True)
                    except ValueError as exc:
                        raise AcpProtocolError(-32602, "Invalid base64 resource") from exc
                    if len(raw) > 5 * 1024 * 1024:
                        raise AcpProtocolError(-32602, "Embedded resource is too large")
                    content = raw.decode("utf-8", errors="replace")
                else:
                    raise AcpProtocolError(-32602, "Resource requires text or blob")
                context_blocks.append(
                    f'<resource uri="{uri}" mime="{mime}">\n{content}\n</resource>'
                )
            elif kind == "resource_link":
                uri = block.get("uri")
                name = block.get("name")
                if not isinstance(uri, str) or not isinstance(name, str):
                    raise AcpProtocolError(-32602, "Resource link requires uri and name")
                context_blocks.append(
                    f'<resource-link uri="{uri}" name="{name}">'
                    "Treść nie została automatycznie odczytana przez agenta."
                    "</resource-link>"
                )
            else:
                raise AcpProtocolError(-32602, f"Unsupported content type: {kind}")
        return "\n\n".join(text_parts), context_blocks
