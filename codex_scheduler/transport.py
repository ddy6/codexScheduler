"""Finite receive bounds shared by the client, profiles, and delivery policies."""

from .registry import SchedulerError


DEFAULT_RECEIVE_BYTES = 8 * 1024 * 1024
MAX_RECEIVE_BYTES = 64 * 1024 * 1024


def receive_limit(value):
    if type(value) is not int or not 1024 <= value <= MAX_RECEIVE_BYTES:
        raise SchedulerError("max_receive_bytes must be an integer between 1024 and 67108864")
    return value


class ResponseTooLarge(SchedulerError):
    def __init__(self, method, limit):
        self.method = method
        self.limit = limit
        super().__init__(f"response_too_large during {method}; receive limit {limit} bytes")

    def details(self, phase):
        return {"code": "response_too_large", "phase": phase,
                "method": self.method, "max_receive_bytes": self.limit}
