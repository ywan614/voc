"""Bounded review retries; keep one result, never union attempts."""
import time

from .pipeline import tag_review
from .run_logging import error_details
from .usage import summarize_usage


def retry_action(record):
    if record["status"] in {"ok", "empty"}:
        return "stop"
    for cause in record.get("error", {}).get("causes", []):
        code = cause.get("status_code")
        if code in {400, 401, 403, 404, 422}:
            return "abort"
        if isinstance(code, int) and 400 <= code < 500 and code not in {408, 409, 429}:
            return "stop"
    return "retry"


def choose_result(best, candidate):
    if (best is None or candidate["status"] in {"ok", "empty"}
            or (not best["insights"] and candidate["insights"])
            or (best["status"] == "error" and candidate["status"] != "error")):
        return candidate
    return best


def iter_attempts(row, model, prompt, examples, *, max_attempts=3, retry_no_thinking=False):
    initial_thinking = getattr(model, "enable_thinking", None)
    run = getattr(model, "run_log", None)
    previous = None
    try:
        for attempt in range(1, max_attempts + 1):
            model.enable_thinking = False if attempt > 1 and retry_no_thinking else initial_thinking
            thinking_mode = "disabled" if model.enable_thinking is False else "provider_default"
            attempt_prompt = prompt
            if previous and previous["status"] in {"empty_result", "needs_review"}:
                attempt_prompt += ("\n上次抽取为空或未通过校验。请重新检查全部原文；功能泛述也按指南"
                                   "提取并标记为泛述。引文必须逐字匹配，属性须符合 schema。"
                                   "确实没有观点时仍可返回空数组，不要编造。")
            started = time.monotonic()
            start_call = len(model.usage_calls)
            if run:
                run.event("attempt_started", attempt=attempt, thinking_mode=thinking_mode)
            try:
                record = tag_review(row, model, attempt_prompt, examples)
            except Exception as exc:
                record = {"review_id": row["review_id"], "metadata": row,
                          "text": row["title"] + "\n" + row["content"],
                          "status": "error", "error_type": type(exc).__name__,
                          "error": error_details(exc), "insights": [], "rejected": []}
            record = {**record, "attempt": attempt, "thinking_mode": thinking_mode,
                      "duration_seconds": round(time.monotonic() - started, 3),
                      "api_usage": model.usage_calls[start_call:]}
            record["usage"] = summarize_usage(record["api_usage"])
            record["retry_action"] = retry_action(record)
            yield record
            if record["retry_action"] != "retry" or attempt == max_attempts:
                break
            delay = min(30, 2 ** min(attempt, 5))
            if run:
                run.event("retry_scheduled", attempt=attempt + 1,
                          reason=record["status"], delay_seconds=delay)
            time.sleep(delay)
            previous = record
    finally:
        model.enable_thinking = initial_thinking
