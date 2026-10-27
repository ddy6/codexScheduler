"""Small, private quota snapshots and structured usage-interruption classification."""

import math

from .storage import utc_now
from .transport import ResponseTooLarge


def error_code(error):
    if not isinstance(error, dict):
        return None
    value = error.get("codexErrorInfo")
    if isinstance(value, str):
        if value in {"usageLimitExceeded", "UsageLimitExceeded"}:
            return "UsageLimitExceeded"
        return value[:128]
    if isinstance(value, dict) and set(value) & {"usageLimitExceeded", "UsageLimitExceeded"}:
        return "UsageLimitExceeded"
    return None


def quota_snapshot(response=None, *, previous=None):
    result = {"observed_at": utc_now(), "availability": "unknown", "reset_at": None, "buckets": [], "complete": False}
    if not isinstance(response, dict):
        return result
    multiple = response.get("rateLimitsByLimitId")
    if isinstance(multiple, dict) and multiple:
        values = list(multiple.items())
    else:
        single = response.get("rateLimits")
        values = [("default", single)] if isinstance(single, dict) else []
    if not values or len(values) > 32:
        return result
    for key, value in values:
        if not isinstance(value, dict):
            result["buckets"].append({"limit_id": str(key)[:128], "windows": [], "availability": "unknown", "reset_at": None})
            continue
        complete, blocked, resets = True, False, []
        bucket = {"limit_id": str(value.get("limitId") or key)[:128], "windows": []}
        reached = value.get("rateLimitReachedType")
        if reached is not None:
            blocked = True
            bucket["limit_reached"] = True
        if value.get("spendControlReached") is True:
            blocked = True
            bucket["spend_blocked"] = True
            resets.append(None)
        individual = value.get("individualLimit")
        if isinstance(individual, dict) and type(individual.get("remainingPercent")) is int and individual["remainingPercent"] <= 0:
            blocked = True
            bucket["spend_blocked"] = True
            resets.append(None)
        for name in ("primary", "secondary"):
            window = value.get(name)
            if window is None:
                continue
            if not isinstance(window, dict):
                complete = False
                continue
            used = window.get("usedPercent")
            minutes = window.get("windowDurationMins")
            reset = window.get("resetsAt")
            if (type(used) not in (int, float) or not math.isfinite(used) or not 0 <= used <= 1000
                    or (minutes is not None and (type(minutes) is not int or not 0 < minutes <= 525600))
                    or (reset is not None and (type(reset) is not int or not 0 <= reset <= 100000000000))):
                complete = False
                continue
            bucket["windows"].append({"name": name, "used_percent": used,
                                      "window_minutes": minutes, "reset_at": reset})
            if used >= 100:
                blocked = True
                resets.append(reset)
        if not bucket["windows"]:
            complete = False
        # An unexplained backend limit flag is not evidence of a timed reset.
        if reached is not None and (reached != "rate_limit_reached"
                                    or not any(w["used_percent"] >= 100 for w in bucket["windows"])):
            resets.append(None)
        bucket["availability"] = "exhausted" if blocked else "available" if complete else "unknown"
        bucket["reset_at"] = max(resets) if blocked and complete and resets and None not in resets else None
        result["buckets"].append(bucket)
    result["complete"] = True
    if previous is not None and not multiple:
        merged = {b["limit_id"]: b for b in previous["buckets"]}
        for bucket in result["buckets"]:
            old = merged.get(bucket["limit_id"])
            if old:
                raw = dict(values[0][1])
                for window in old["windows"]:
                    if window["name"] not in raw:
                        raw[window["name"]] = {"usedPercent": window["used_percent"],
                                               "windowDurationMins": window["window_minutes"], "resetsAt": window["reset_at"]}
                if old.get("limit_reached") and "rateLimitReachedType" not in raw:
                    raw["rateLimitReachedType"] = "rate_limit_reached"
                if old.get("spend_blocked") and raw.get("spendControlReached") is not False:
                    raw["spendControlReached"] = True
                bucket.update(quota_snapshot({"rateLimits": raw})["buckets"][0])
        merged.update({b["limit_id"]: b for b in result["buckets"]})
        result["buckets"] = list(merged.values())[:32]
        result["complete"] = previous.get("complete", False) and len(merged) <= 32
    blocked = [b for b in result["buckets"] if b["availability"] == "exhausted"]
    complete = result["complete"] and all(b["availability"] != "unknown" for b in result["buckets"])
    result["availability"] = "exhausted" if blocked else "available" if complete else "unknown"
    if blocked and complete and all(b["reset_at"] is not None for b in blocked):
        result["reset_at"] = max(b["reset_at"] for b in blocked)
    return result


def read_quota(adapter):
    """Unsupported endpoints remain unknown; neither an error nor time implies refill."""
    from .registry import SchedulerError
    from .delivery import NeedsOperator

    if not hasattr(adapter, "rate_limits"):
        return quota_snapshot()
    try:
        return quota_snapshot(adapter.rate_limits())
    except (NeedsOperator, ResponseTooLarge):
        raise
    except SchedulerError:
        return quota_snapshot()
