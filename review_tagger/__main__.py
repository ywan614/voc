import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import random
import time

from .run_logging import RunLog, error_details

from .config import load_settings
from .model import create_model
from .pipeline import load_examples, read_reviews, write_tagged_csv
from .prompts import build_prompt
from .retry import iter_attempts, choose_result
from .usage import summarize_usage

ROOT = Path(__file__).resolve().parent.parent


def main():
    parser = argparse.ArgumentParser(description="LangExtract + Qwen 评论逐观点打标")
    parser.add_argument("--input", type=Path, default=ROOT / "data/sample.csv")
    parser.add_argument("--config", type=Path, default=ROOT / "config.ini")
    parser.add_argument("--guide", type=Path, default=ROOT / "docs/review_tagging_guide.md")
    parser.add_argument("--examples", type=Path, default=ROOT / "review_tagger/examples.json")
    parser.add_argument("--output-dir", type=Path)
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--limit", type=int, help="仅处理前 N 条，用于小批量验证")
    selection.add_argument("--sample", type=int, help="随机抽取 N 条，不放回")
    selection.add_argument("--last", type=int, help="仅处理最后 N 条评论")
    parser.add_argument("--seed", type=int, help="随机种子，便于复现抽样")
    parser.add_argument("--dry-run", action="store_true", help="检查输入、配置、示例，不调用 API")
    parser.add_argument("--no-thinking", action="store_true", help="关闭 Qwen 思考模式")
    parser.add_argument("--max-attempts", type=int, default=3, help="每条最多尝试次数，含首次（默认 3）")
    parser.add_argument("--retry-no-thinking", action="store_true", help="仅在重试时关闭 Qwen 思考模式")
    args = parser.parse_args()
    output = args.output_dir or ROOT / "data/tagging_runs" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    # Refuse reuse before writing any artifacts from a different run.
    try:
        output.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"无法创建运行目录：{output} ({type(exc).__name__})", file=sys.stderr)
        return 2
    if any((output / name).exists() for name in ("reviews.csv", "reviews.jsonl", "attempts.jsonl", "run.log", "events.jsonl", "run_summary.json")):
        print(f"运行目录已有结果或日志，请使用新目录：{output}", file=sys.stderr)
        return 2
    try:
        with RunLog(output, args.config) as run:
            print(f"运行日志：{output / 'run.log'}；诊断事件：{output / 'events.jsonl'}", flush=True)
            run.event("run_started", arguments=vars(args))
            code = run_batch(args, output, run)
            run.event("run_finished", exit_code=code)
            return code
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"运行失败 ({type(exc).__name__})，请检查日志及目录是否可写：{output / 'run.log'}", file=sys.stderr)
        return 2


def run_batch(args, output, run):
    if args.max_attempts <= 0:
        raise ValueError("--max-attempts 必须大于 0")
    if args.limit is not None and args.limit <= 0:
        raise ValueError("--limit 必须大于 0")
    if args.sample is not None and args.sample <= 0:
        raise ValueError("--sample 必须大于 0")
    if args.last is not None and args.last <= 0:
        raise ValueError("--last 必须大于 0")
    seed = args.seed if args.seed is not None else random.SystemRandom().randrange(2**32)
    try:
        all_rows = read_reviews(args.input)
        rows = all_rows
        if rows and "all_tags" in rows[0]:
            raise ValueError("输入已包含 all_tags 列，请使用原始 CSV")
        if not rows:
            raise ValueError("输入 CSV 没有评论")
        if args.limit:
            rows = rows[:args.limit]
        if args.last:
            rows = rows[-args.last:]
        if args.sample:
            if args.sample > len(rows):
                raise ValueError("--sample 不能超过输入评论数")
            rows = random.Random(seed).sample(rows, args.sample)
        settings = load_settings(args.config)
        run.redact.add(settings.api_key)
        examples = load_examples(args.examples)
        prompt = build_prompt(args.guide)
    except (OSError, ValueError) as exc:
        run.event("validation_failed", error=error_details(exc))
        print(f"输入/配置检查失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        print(error_details(exc)["traceback"], file=sys.stderr)
        return 2
    run.event("run_ready", model=settings.model_name,
              thinking_mode="disabled" if args.no_thinking else "provider_default",
              sample_seed=seed if args.sample else None,
              selected_review_ids=[row["review_id"] for row in rows])
    if args.dry_run:
        print(f"检查通过：{len(rows)} 条评论，{len(examples)} 个示例；未调用 API。")
        return 0
    # ponytail: input/results stay in memory; use shards or SQLite for million-row runs.
    csv_records = {}
    model = None
    state = "running"
    total_attempts = 0
    status_counts = Counter()
    run_usage = summarize_usage([])

    def save_summary():
        summary = {
            "input": str(args.input.resolve()), "model": settings.model_name,
            "thinking_mode": "disabled" if args.no_thinking else "provider_default",
            "retry_no_thinking": args.retry_no_thinking, "max_attempts": args.max_attempts,
            "sample_seed": seed if args.sample else None,
            "processed_reviews": len(csv_records), "selected_reviews": len(rows),
            "status_counts": dict(status_counts), "total_attempts": total_attempts,
            "reviews_needing_attention": sum(v for k, v in status_counts.items() if k not in {"ok", "empty"}),
            "state": state, "usage": run_usage,
        }
        temporary = output / "run_summary.json.tmp"
        temporary.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(output / "run_summary.json")

    try:
        with (output / "reviews.jsonl").open("x", encoding="utf-8") as stream, \
                (output / "attempts.jsonl").open("x", encoding="utf-8") as attempts_stream:
            model = create_model(settings, enable_thinking=False if args.no_thinking else None)
            model.run_log = run
            for index, row in enumerate(rows, 1):
                run.review_id = row["review_id"]
                started = time.monotonic()
                run.event("review_started", index=index, selected_reviews=len(rows),
                          text=row["title"] + "\n" + row["content"])
                print(f"[{index}/{len(rows)}] 开始 review_id={row['review_id']}", flush=True)
                start_call = len(model.usage_calls)
                best = last = None
                interrupted = False
                try:
                    for attempt in iter_attempts(row, model, prompt, examples,
                                                 max_attempts=args.max_attempts,
                                                 retry_no_thinking=args.retry_no_thinking):
                        attempts_stream.write(json.dumps(run.clean(attempt), ensure_ascii=False) + "\n")
                        attempts_stream.flush()
                        total_attempts += 1
                        last = attempt
                        best = choose_result(best, attempt)
                        run.event("attempt_finished", attempt=attempt["attempt"], status=attempt["status"],
                                  thinking_mode=attempt["thinking_mode"], usage=attempt["usage"])
                        if attempt["status"] == "error":
                            run.event("review_failed", attempt=attempt["attempt"], error=attempt["error"])
                            print(f"失败 review_id={row['review_id']}: {attempt['error']['message']}", file=sys.stderr)
                            print(attempt["error"]["traceback"], file=sys.stderr)
                        for rejected in attempt["rejected"]:
                            run.event("extraction_rejected", attempt=attempt["attempt"], **rejected)
                            print(f"需复核 review_id={row['review_id']}: {json.dumps(rejected, ensure_ascii=False)}", file=sys.stderr)
                        print(f"  尝试 {attempt['attempt']}/{args.max_attempts}: {attempt['status']}", flush=True)
                except KeyboardInterrupt:
                    interrupted = True
                    raise
                finally:
                    if best is not None:
                        calls = model.usage_calls[start_call:]
                        record = {**best, "selected_attempt": best["attempt"],
                                  "attempt_count": last["attempt"], "last_status": last["status"],
                                  "retry_action": last["retry_action"],
                                  "interrupted": interrupted,
                                  "retry_exhausted": not interrupted and last["retry_action"] == "retry"
                                                     and last["attempt"] == args.max_attempts,
                                  "duration_seconds": round(time.monotonic() - started, 3),
                                  "usage": summarize_usage(calls), "api_usage": calls}
                        # Keep the original fields for CSV, independent of diagnostic redaction.
                        csv_records[row["review_id"]] = record
                        stream.write(json.dumps(run.clean(record), ensure_ascii=False) + "\n")
                        stream.flush()
                        status_counts[record["status"]] += 1
                        for key, value in record["usage"].items():
                            if key == "usage_complete":
                                run_usage[key] = run_usage[key] and value
                            else:
                                run_usage[key] += value
                        run.event("review_finished", index=index, status=record["status"],
                                  selected_attempt=record["selected_attempt"], attempt_count=record["attempt_count"],
                                  duration_seconds=record["duration_seconds"], usage=record["usage"],
                                  accepted_count=len(record["insights"]), rejected_count=len(record["rejected"]))
                        save_summary()
                if last["retry_action"] == "abort":
                    state = "aborted"
                    run.event("batch_aborted", error=last["error"])
                    print("服务配置或请求被拒绝，已停止后续处理；详见 attempts.jsonl。", file=sys.stderr)
                    break
                print(f"[{index}/{len(rows)}] review_id={row['review_id']} {record['status']} tokens={record['usage']['total_tokens']}", flush=True)
            else:
                state = "completed"
    except KeyboardInterrupt:
        state = "interrupted"
        raise
    except Exception:
        state = "failed"
        raise
    finally:
        if model is not None:
            run_usage = summarize_usage(model.usage_calls)
        write_tagged_csv(output / "reviews.csv", all_rows, csv_records)
        save_summary()
    run.review_id = None
    failures = sum(v for k, v in status_counts.items() if k not in {"ok", "empty"})
    print(f"结果：{output / 'reviews.csv'}；详细结果：{output / 'reviews.jsonl'}；需检查 {failures} 条")
    print(f"Token 用量：输入 {run_usage['prompt_tokens']} / 输出 {run_usage['completion_tokens']} / 总计 {run_usage['total_tokens']}")
    if not run_usage["usage_complete"]:
        print("部分调用未返回完整 usage；上述数值仅为已报告用量。")
    return 2 if state == "aborted" else 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
