import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import random
import time

from .run_logging import RunLog, error_details

from .config import load_settings
from .model import create_model
from .pipeline import load_examples, read_reviews, tag_review
from .prompts import build_prompt
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
    args = parser.parse_args()
    output = args.output_dir or ROOT / "data/tagging_runs" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    # Refuse reuse before writing any artifacts from a different run.
    try:
        output.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"无法创建运行目录：{output} ({type(exc).__name__})", file=sys.stderr)
        return 2
    if any((output / name).exists() for name in ("reviews.jsonl", "run.log", "events.jsonl", "run_summary.json")):
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
    if args.limit is not None and args.limit <= 0:
        raise ValueError("--limit 必须大于 0")
    if args.sample is not None and args.sample <= 0:
        raise ValueError("--sample 必须大于 0")
    if args.last is not None and args.last <= 0:
        raise ValueError("--last 必须大于 0")
    seed = args.seed if args.seed is not None else random.SystemRandom().randrange(2**32)
    try:
        rows = read_reviews(args.input)
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
    # Exclusive creation prevents accidental overwrite; flush after every review.
    with (output / "reviews.jsonl").open("x", encoding="utf-8") as stream:
        model = create_model(settings, enable_thinking=False if args.no_thinking else None)
        model.run_log = run
        failures = 0
        for index, row in enumerate(rows, 1):
            run.review_id = row["review_id"]
            started = time.monotonic()
            run.event("review_started", index=index, selected_reviews=len(rows),
                      text=row["title"] + "\n" + row["content"])
            print(f"[{index}/{len(rows)}] 开始 review_id={row['review_id']}", flush=True)
            start_call = len(model.usage_calls)
            try:
                record = tag_review(row, model, prompt, examples)
            except Exception as exc:
                record = {"review_id": row["review_id"], "metadata": row,
                          "text": row["title"] + "\n" + row["content"],
                          "status": "error", "error_type": type(exc).__name__,
                          "error": run.clean(error_details(exc)),
                          "insights": [], "rejected": []}
                run.event("review_failed", error=record["error"])
                print(f"失败 review_id={row['review_id']}: {record['error']['message']}", file=sys.stderr)
                print(record["error"]["traceback"], file=sys.stderr)
            calls = model.usage_calls[start_call:]
            record["usage"] = summarize_usage(calls)
            record["api_usage"] = calls
            failures += record["status"] in {"error", "needs_review"}
            record["duration_seconds"] = round(time.monotonic() - started, 3)
            for rejected in record["rejected"]:
                run.event("extraction_rejected", **rejected)
                print(f"需复核 review_id={row['review_id']}: {json.dumps(rejected, ensure_ascii=False)}", file=sys.stderr)
            stream.write(json.dumps(run.clean(record), ensure_ascii=False) + "\n")
            stream.flush()
            run.event("review_finished", index=index, status=record["status"],
                      duration_seconds=record["duration_seconds"], usage=record["usage"],
                      accepted_count=len(record["insights"]), rejected_count=len(record["rejected"]))
            summary = {
                "input": str(args.input.resolve()), "model": settings.model_name,
                "thinking_mode": "disabled" if args.no_thinking else "provider_default",
                "sample_seed": seed if args.sample else None,
                "selected_review_ids": [r["review_id"] for r in rows],
                "processed_reviews": index, "selected_reviews": len(rows),
                "reviews_needing_attention": failures,
                "usage": summarize_usage(model.usage_calls),
            }
            temporary = output / "run_summary.json.tmp"
            temporary.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            temporary.replace(output / "run_summary.json")
            print(f"[{index}/{len(rows)}] review_id={row['review_id']} {record['status']} tokens={record['usage']['total_tokens']}", flush=True)
    run.review_id = None
    print(f"结果：{output / 'reviews.jsonl'}；需检查 {failures} 条")
    usage = summarize_usage(model.usage_calls)
    print(f"Token 用量：输入 {usage['prompt_tokens']} / 输出 {usage['completion_tokens']} / 总计 {usage['total_tokens']}")
    if not usage["usage_complete"]:
        print("部分调用未返回完整 usage；上述数值仅为已报告用量。")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
