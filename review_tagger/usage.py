"""服务端实际返回的 token 用量；不以字符数估算。"""


def summarize_usage(calls):
    totals = {key: 0 for key in ("prompt_tokens", "completion_tokens", "total_tokens")}
    missing = 0
    for call in calls:
        usage = call.get("usage") or {}
        if any(not isinstance(usage.get(key), int) for key in totals):
            missing += 1
        for key in totals:
            value = usage.get(key)
            if isinstance(value, int):
                totals[key] += value
    return {**totals, "api_calls": len(calls), "calls_without_complete_usage": missing,
            "usage_complete": missing == 0}
