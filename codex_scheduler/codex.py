"""Bounded RPC access to a dedicated local Codex worker over a Unix socket."""

import json
import time

from .delivery import NeedsOperator
from .registry import SchedulerError


class CodexAdapter:
    @staticmethod
    def require_available():
        try:
            from websockets.sync.client import unix_connect
        except ImportError as error:
            raise SchedulerError("Install the optional WebSocket transport before running delivery") from error
        return unix_connect

    def __init__(self, spec, *, timeout=5):
        self.spec = spec
        self.timeout = timeout
        self.connection = None
        self.sequence = 0

    def __enter__(self):
        unix_connect = self.require_available()
        self.connection = unix_connect(self.spec["socket_path"], open_timeout=self.timeout,
                                       close_timeout=1, max_size=1048576)
        try:
            self.call("initialize", {
                "clientInfo": {"name": "codex_scheduler", "version": "0.1"},
                "capabilities": {"experimentalApi": True},
            })
            self.connection.send(json.dumps({"method": "initialized", "params": {}}))
        except Exception:
            self.connection.close()
            raise
        return self

    def __exit__(self, *_):
        if self.connection:
            self.connection.close()

    def call(self, method, params):
        self.sequence += 1
        request_id = self.sequence
        self.connection.send(json.dumps({"id": request_id, "method": method, "params": params}))
        deadline = time.monotonic() + self.timeout
        for _ in range(2048):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SchedulerError("Codex RPC timed out")
            message = json.loads(self.connection.recv(timeout=remaining))
            if "method" in message and "id" in message:
                # Do not grant approval, answer user questions, or execute dynamic tools.
                raise NeedsOperator("Codex requires operator input: " + message["method"])
            if message.get("id") == request_id:
                if "error" in message:
                    raise SchedulerError("Codex rejected " + method)
                return message["result"]
        raise SchedulerError("Codex RPC notification allowance exhausted")

    def snapshot(self, thread_id):
        # Omit all overrides: resume must retain the worker's existing permissions.
        return self.call("thread/resume", {"threadId": thread_id, "excludeTurns": True})

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
        matches = []
        cursor = None
        seen_turns = set()
        for _ in range(5):
            params = {"threadId": event["worker_thread"], "limit": 20,
                      "itemsView": "full", "sortDirection": "desc"}
            if cursor:
                params["cursor"] = cursor
            page = self.call("thread/turns/list", params)
            for turn in page["data"]:
                if turn["id"] in seen_turns:
                    continue
                seen_turns.add(turn["id"])
                if turn.get("itemsView", "full") != "full":
                    raise NeedsOperator("Worker history lacks full input evidence")
                users = [item for item in turn["items"] if item["type"] == "userMessage"]
                matched = [item for item in users if any(
                    part.get("type") == "text" and part.get("text") == event["request_text"]
                    for part in item["content"]
                )]
                if matched:
                    if len(users) != 1 or len(matched) != 1 or len(matched[0]["content"]) != 1:
                        raise NeedsOperator("Completion was mixed with other user input")
                    matches.append({"id": turn["id"], "status": turn["status"]})
            cursor = page.get("nextCursor")
            if not cursor:
                return matches
        # Truncated history cannot establish uniqueness or authorize a retry.
        raise NeedsOperator("Worker history exceeded the bounded scan")
