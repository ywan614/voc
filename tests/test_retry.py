import contextlib
import csv
import io
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from review_tagger.__main__ import main
from review_tagger.pipeline import tag_review, write_tagged_csv


class RetryIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.config = self.folder / "config.ini"
        self.config.write_text(
            "[model]\nmodel_name=qwen-test\nmodel_base_url=https://example.com/v1\n"
            "[openai]\nopenai_api_key=private-test-key\n", encoding="utf-8")
        self.rows = [
            {"review_id": "001", "title": '中文,"标题"', "content": "soft\nbreathable", "tags": "old; tag"},
            {"review_id": "002", "title": "Good", "content": "very good", "tags": "original"},
            {"review_id": "003", "title": "Thin", "content": "too thin", "tags": ""},
        ]
        self.input = self.folder / "input.csv"
        self.write_input()
        self.output = self.folder / "run"
        self.model = SimpleNamespace(usage_calls=[], enable_thinking=None)

    def write_input(self):
        with self.input.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(self.rows[0]))
            writer.writeheader()
            writer.writerows(self.rows)

    def invoke(self, tag, *extra):
        args = ["review_tagger", "--config", str(self.config), "--input", str(self.input),
                "--output-dir", str(self.output), *extra]
        with (patch.object(sys, "argv", args),
              patch("review_tagger.__main__.create_model", return_value=self.model),
              patch("review_tagger.retry.tag_review", side_effect=tag),
              patch("review_tagger.retry.time.sleep"),
              contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO())):
            return main()

    def record(self, row, status="ok", quote="soft"):
        self.model.usage_calls.append({"usage": {
            "prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}})
        return {"review_id": row["review_id"], "metadata": row,
                "text": row["title"] + "\n" + row["content"], "status": status,
                "insights": [{"extraction_text": quote}] if status in {"ok", "needs_review"} else [],
                "rejected": [{"reason": "not_exactly_grounded"}] if status == "needs_review" else []}

    def jsonl(self, name):
        return [json.loads(line) for line in (self.output / name).read_text().splitlines()]

    def csv_rows(self):
        with (self.output / "reviews.csv").open(encoding="utf-8", newline="") as stream:
            return list(csv.DictReader(stream))

    def test_empty_then_success_selects_retry_and_counts_all_usage(self):
        statuses = iter(["empty_result", "ok"])
        self.assertEqual(self.invoke(lambda row, *_: self.record(row, next(statuses)), "--limit", "1"), 0)
        attempts = self.jsonl("attempts.jsonl")
        self.assertEqual([r["attempt"] for r in attempts], [1, 2])
        self.assertEqual([r["usage"]["total_tokens"] for r in attempts], [12, 12])
        final, = self.jsonl("reviews.jsonl")
        self.assertEqual((final["status"], final["selected_attempt"], final["attempt_count"]), ("ok", 2, 2))
        self.assertEqual(final["usage"]["total_tokens"], 24)
        self.assertEqual(len(final["api_usage"]), 2)
        self.assertFalse(final["retry_exhausted"])
        self.assertEqual(json.loads(self.csv_rows()[0]["all_tags"]), final["insights"])

    def test_empty_results_stop_at_limit_and_report_attention(self):
        self.assertEqual(self.invoke(lambda row, *_: self.record(row, "empty_result"), "--limit", "1"), 1)
        self.assertEqual(len(self.jsonl("attempts.jsonl")), 3)
        final, = self.jsonl("reviews.jsonl")
        self.assertEqual(final["status"], "empty_result")
        self.assertTrue(final["retry_exhausted"])
        self.assertEqual(final["attempt_count"], 3)
        self.assertEqual(self.csv_rows()[0]["all_tags"], "[]")
        summary = json.loads((self.output / "run_summary.json").read_text())
        self.assertEqual(summary["status_counts"]["empty_result"], 1)
        self.assertEqual(summary["processed_reviews"], 1)
        self.assertEqual(summary["usage"]["total_tokens"], 36)

    def test_empty_input_skips_extraction_and_retry(self):
        self.rows[0].update(title="", content=" \n")
        self.write_input()
        with patch("review_tagger.pipeline.lx.extract") as extract:
            self.assertEqual(self.invoke(tag_review, "--limit", "1"), 0)
        extract.assert_not_called()
        final, = self.jsonl("reviews.jsonl")
        self.assertEqual((final["status"], final["attempt_count"]), ("empty", 1))
        self.assertEqual(final["usage"]["api_calls"], 0)
        self.assertEqual(len(self.jsonl("attempts.jsonl")), 1)
        self.assertEqual(self.csv_rows()[0]["all_tags"], "[]")

    def test_timeout_recovers_unless_retries_are_disabled(self):
        for maximum, expected_code in [(2, 0), (1, 1)]:
            with self.subTest(max_attempts=maximum):
                self.output = self.folder / f"run-{maximum}"
                self.model.usage_calls.clear()

                def tag(row, *_):
                    if not self.model.usage_calls:
                        self.model.usage_calls.append({"usage": None})
                        raise TimeoutError("timed out")
                    return self.record(row)

                self.assertEqual(self.invoke(tag, "--limit", "1", "--max-attempts", str(maximum)), expected_code)
                final, = self.jsonl("reviews.jsonl")
                self.assertEqual(final["attempt_count"], maximum)
                self.assertEqual(final["status"], "ok" if maximum == 2 else "error")
                self.assertFalse(final["usage"]["usage_complete"])

    def test_first_partial_survives_later_partial_and_error_without_union(self):
        count = 0

        def tag(row, *_):
            nonlocal count
            count += 1
            if count == 3:
                self.model.usage_calls.append({"usage": None})
                raise TimeoutError("timed out")
            return self.record(row, "needs_review", "soft" if count == 1 else "breathable")

        self.assertEqual(self.invoke(tag, "--limit", "1"), 1)
        final, = self.jsonl("reviews.jsonl")
        self.assertEqual((final["status"], final["selected_attempt"], final["attempt_count"]), ("needs_review", 1, 3))
        self.assertEqual(final["insights"], [{"extraction_text": "soft"}])
        self.assertEqual(json.loads(self.csv_rows()[0]["all_tags"]), final["insights"])
        self.assertTrue(final["retry_exhausted"])
        self.assertFalse(final["usage"]["usage_complete"])
        self.assertEqual(final["usage"]["api_calls"], 3)

    def test_fatal_wrapped_provider_error_aborts_next_review(self):
        def tag(row, *_):
            try:
                error = RuntimeError("invalid credentials private-test-key")
                error.status_code = 401
                raise error
            except RuntimeError as error:
                raise ValueError("provider wrapper") from error

        self.assertEqual(self.invoke(tag), 2)
        final, = self.jsonl("reviews.jsonl")
        self.assertEqual((final["review_id"], final["status"], final["attempt_count"]), ("001", "error", 1))
        self.assertEqual(len(self.jsonl("attempts.jsonl")), 1)
        self.assertEqual([row["all_tags"] for row in self.csv_rows()], ["", "", ""])
        for path in self.output.iterdir():
            self.assertNotIn("private-test-key", path.read_text())

    def test_retry_no_thinking_resets_for_the_next_review(self):
        modes = []
        prompts = []

        def tag(row, model, prompt, *_):
            modes.append((row["review_id"], model.enable_thinking))
            prompts.append(prompt)
            return self.record(row, "empty_result" if len(modes) == 1 else "ok")

        self.assertEqual(self.invoke(tag, "--limit", "2", "--retry-no-thinking"), 0)
        self.assertEqual(modes, [("001", None), ("001", False), ("002", None)])
        self.assertNotEqual(prompts[0], prompts[1])
        self.assertEqual(prompts[0], prompts[2])
        self.assertEqual([r["thinking_mode"] for r in self.jsonl("attempts.jsonl")],
                         ["provider_default", "disabled", "provider_default"])

    def test_sample_export_keeps_original_fields_order_and_unselected_rows(self):
        with patch("review_tagger.__main__.write_tagged_csv", wraps=write_tagged_csv) as export:
            self.assertEqual(self.invoke(lambda row, *_: self.record(row), "--sample", "1", "--seed", "7"), 0)
        self.assertEqual(export.call_count, 1)
        actual = self.csv_rows()
        self.assertEqual(list(actual[0]), list(self.rows[0]) + ["all_tags"])
        self.assertEqual([{key: row[key] for key in self.rows[0]} for row in actual], self.rows)
        final, = self.jsonl("reviews.jsonl")
        for row in actual:
            self.assertEqual(bool(row["all_tags"]), row["review_id"] == final["review_id"])

    def test_diagnostic_redaction_does_not_change_csv_quotes(self):
        quote = "Bearer of good news"
        self.rows[0]["content"] = quote
        self.write_input()
        self.assertEqual(self.invoke(lambda row, *_: self.record(row, quote=quote), "--limit", "1"), 0)
        row = self.csv_rows()[0]
        self.assertEqual(row["content"], quote)
        self.assertEqual(json.loads(row["all_tags"])[0]["extraction_text"], quote)
        self.assertNotIn(quote, (self.output / "attempts.jsonl").read_text())

    def test_interrupt_during_retry_exports_previous_partial(self):
        count = 0

        def tag(row, *_):
            nonlocal count
            count += 1
            if count == 2:
                raise KeyboardInterrupt
            return self.record(row, "needs_review")

        self.assertEqual(self.invoke(tag, "--limit", "1"), 130)
        final, = self.jsonl("reviews.jsonl")
        self.assertEqual(final["selected_attempt"], 1)
        self.assertEqual(final["insights"], [{"extraction_text": "soft"}])
        self.assertEqual(json.loads(self.csv_rows()[0]["all_tags"]), final["insights"])
        self.assertEqual(len(self.csv_rows()), len(self.rows))


if __name__ == "__main__":
    unittest.main()
