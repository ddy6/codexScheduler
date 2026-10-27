"""Bounded RPC access to a dedicated local Codex worker over a Unix socket."""

import json
import time

from .delivery import NeedsOperator
from .registry import SchedulerError
from .usage import error_code
from .transport import DEFAULT_RECEIVE_BYTES, ResponseTooLarge, receive_limit


class CodexAdapter:
    @staticmethod
    def require_available():
        try:
            from websockets.sync.client import unix_connect
        except ImportError as error:
            raise SchedulerError("Install the optional WebSocket transport before running delivery") from error
        return unix_connect

    def __init__(self, spec, *, timeout=5, on_notification=None):
        self.spec = spec
        self.timeout = timeout
        self.connection = None
        self.sequence = 0
        self.on_notification = on_notification
        self.max_receive_bytes = receive_limit(spec.get("max_receive_bytes", DEFAULT_RECEIVE_BYTES))

    def __enter__(self):
        unix_connect = self.require_available()
        self.connection = unix_connect(self.spec["socket_path"], open_timeout=self.timeout,
                                       close_timeout=1, max_size=self.max_receive_bytes)
        try:
            self.call("initialize", {
                "clientInfo": {"name": "codex_scheduler", "version": "0.1"},
                "capabilities": {"experimentalApi": True},
            })
            self._send("initialized", {"method": "initialized", "params": {}})
        except Exception:
            self.connection.close()
            raise
        return self

    def __exit__(self, *_):
        if self.connection:
            self.connection.close()

    def _oversized_close(self, error):
        # A locally sent 1009 means our receiver rejected an incoming message.
        # A peer's 1009 alone can instead mean it rejected our outgoing request.
        return (getattr(getattr(error, "sent", None), "code", None) == 1009
                and getattr(error, "rcvd_then_sent", False) is not True)

    def _receive(self, method, timeout):
        try:
            raw = self.connection.recv(timeout=timeout)
        except Exception as error:
            if self._oversized_close(error):
                raise ResponseTooLarge(method, self.max_receive_bytes) from error
            raise
        size = len(raw.encode("utf-8")) if isinstance(raw, str) else len(raw)
        if size > self.max_receive_bytes:
            raise ResponseTooLarge(method, self.max_receive_bytes)
        return json.loads(raw)

    def _send(self, method, message):
        try:
            self.connection.send(json.dumps(message))
        except Exception as error:
            if self._oversized_close(error):
                raise ResponseTooLarge(method, self.max_receive_bytes) from error
            raise

    def call(self, method, params):
        self.sequence += 1
        request_id = self.sequence
        self._send(method, {"id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + self.timeout
        for _ in range(2048):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SchedulerError("Codex RPC timed out")
            message = self._receive(method, remaining)
            if "method" in message and "id" in message:
                # Do not grant approval, answer user questions, or execute dynamic tools.
                raise NeedsOperator("Codex requires operator input: " + message["method"])
            if "method" in message and self.on_notification:
                self.on_notification(message)
            if message.get("id") == request_id:
                if "error" in message:
                    raise SchedulerError("Codex rejected " + method)
                return message["result"]
        raise SchedulerError("Codex RPC notification allowance exhausted")

    def read_notification(self, *, timeout):
        """Receive existing events; never send a prompt or answer a server request."""
        try:
            message = self._receive("notifications", timeout)
        except TimeoutError:
            return False
        if "method" in message and "id" in message:
            raise NeedsOperator("Codex requires operator input")
        if "method" in message and self.on_notification:
            self.on_notification(message)
        return True

    def snapshot(self, thread_id):
        # Omit all overrides: resume must retain the worker's existing permissions.
        return self.call("thread/resume", {"threadId": thread_id, "excludeTurns": True})

    def rate_limits(self):
        return self.call("account/rateLimits/read", {})

    def latest_turn(self, thread_id):
        page = self.call("thread/turns/list", {"threadId": thread_id, "limit": 1,
                                              "itemsView": "notLoaded", "sortDirection": "desc"})
        if not page["data"]:
            return None
        turn = page["data"][0]
        return {"id": turn["id"], "status": turn["status"], "error_code": error_code(turn.get("error"))}

    def has_queued_input(self, thread_id):
        result = self.call("thread/queue/list", {"threadId": thread_id})
        return bool(result["data"] or result.get("nextCursor"))

    def start(self, event):
        # The controller owns this dedicated worker. This must never target a live user chat.
        result = self.call("turn/start", {
            "threadId": event["worker_thread"], "clientUserMessageId": event["event_id"],
            "input": [{"type": "text", "text": event["request_text"]}],
        })
        return result["turn"]["id"]

    def find_event(self, event):
        """Verify an acknowledged turn; search conservatively only when its ID is unknown."""
        matches = []
        cursor = None
        seen_turns = set()
        reads = 0
        deadline = time.monotonic() + 15

        def read(method, params):
            nonlocal reads
            if reads >= 32 or time.monotonic() >= deadline:
                raise NeedsOperator("Completion verification exceeded its bounded reads or time")
            reads += 1
            try:
                return self.call(method, params)
            except (NeedsOperator, ResponseTooLarge):
                raise
            except SchedulerError as error:
                raise NeedsOperator("Completion evidence unavailable from " + method + "; no resend authorized") from error

        acknowledged = event.get("turn_id")
        for _ in range(5):
            params = {"threadId": event["worker_thread"], "limit": 20,
                      "itemsView": "notLoaded", "sortDirection": "desc"}
            if cursor:
                params["cursor"] = cursor
            page = read("thread/turns/list", params)
            for turn in page["data"]:
                if turn["id"] in seen_turns:
                    continue
                seen_turns.add(turn["id"])
                if acknowledged and turn["id"] != acknowledged:
                    continue
                first = None
                if not acknowledged:
                    # Our turn starts with this exact user input. Unrelated turns
                    # need only their first item, never their command output.
                    first = read("thread/items/list", {"threadId": event["worker_thread"], "turnId": turn["id"],
                                                       "limit": 1, "sortDirection": "asc"})
                    entries = first["data"]
                    if not entries or entries[0].get("turnId") != turn["id"]:
                        if first.get("nextCursor"):
                            raise NeedsOperator("Candidate turn input is incomplete")
                        continue
                    item = entries[0]["item"]
                    if item.get("type") != "userMessage":
                        raise NeedsOperator("Candidate turn does not expose its initial user input")
                    if not any(part.get("type") == "text" and part.get("text") == event["request_text"]
                               for part in item.get("content", [])):
                        continue
                if self._verify_turn_input(read, event, turn["id"], first):
                    match = {"id": turn["id"], "status": turn["status"]}
                    code = error_code(turn.get("error"))
                    if code:
                        match["error_code"] = code
                    matches.append(match)
                if acknowledged:
                    return matches
            cursor = page.get("nextCursor")
            if not cursor:
                return matches
        # Truncated history cannot establish uniqueness or authorize a retry.
        raise NeedsOperator("Worker history exceeded the bounded scan")

    def _verify_turn_input(self, read, event, turn_id, first=None):
        users, seen, cursor = [], set(), None
        for page_number in range(5):
            params = {"threadId": event["worker_thread"], "turnId": turn_id, "limit": 20, "sortDirection": "asc"}
            if cursor:
                params["cursor"] = cursor
            page = first if page_number == 0 and first is not None else read("thread/items/list", params)
            for entry in page["data"]:
                item = entry["item"]
                item_id = item.get("id")
                if entry.get("turnId") != turn_id or not item_id or item_id in seen:
                    raise NeedsOperator("Turn item identity is missing, duplicated, or out of scope")
                seen.add(item_id)
                if item.get("type") == "userMessage":
                    users.append(item)
            cursor = page.get("nextCursor")
            if not cursor:
                if len(users) != 1 or len(users[0].get("content", [])) != 1:
                    raise NeedsOperator("Completion was mixed with other user input or lacks input evidence")
                content = users[0]["content"][0]
                if content.get("type") != "text" or content.get("text") != event["request_text"]:
                    raise NeedsOperator("Acknowledged turn input disagrees with the saved completion")
                return True
        raise NeedsOperator("Acknowledged turn input exceeded the bounded item pages")
