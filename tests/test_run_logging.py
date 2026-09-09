import contextlib
import io
import json
import logging
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from openai.types.chat import ChatCompletion

from review_tagger.__main__ import main
from review_tagger.model import UsageTrackingModel
from review_tagger.run_logging import RunLog


class RunLoggingTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.config = self.folder / "config.ini"
        self.config.write_text("[model]\nmodel_name=qwen-test\nmodel_base_url=https://example.com/v1\n"
                               "[openai]\nopenai_api_key=private-test-key\n", encoding="utf-8")
        self.input = self.folder / "input.csv"
        self.input.write_text("review_id,title,content\n001,Thin,too thin\n002,Good,very good\n", encoding="utf-8")
        self.output = self.folder / "run"

    def invoke(self, *extra):
        args = ["review_tagger", "--config", str(self.config), "--input", str(self.input),
                "--output-dir", str(self.output), "--max-attempts", "1", *extra]
        with patch.object(sys, "argv", args), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return main()

    def events(self):
        return [json.loads(line) for line in (self.output / "events.jsonl").read_text().splitlines()]

    def test_console_capture_flush_redaction_and_restore(self):
        self.output.mkdir()
        before = sys.stdout, sys.stderr, logging.getLogger().level
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            with RunLog(self.output, self.config) as run:
                run.review_id = "001"
                sys.stdout.write("private-test-")
                sys.stdout.write("key\n")
                print("stderr output", file=sys.stderr)
                logging.getLogger("openai").info("Retrying request in 0.2 seconds")
                run.event("test", output="Bearer private-test-key", multiline="one\ntwo")
                # Visible on disk while the run is still open.
                log = (self.output / "run.log").read_text()
                self.assertIn("stderr output", log)
                self.assertIn("Retrying request", log)
                self.assertIn("review_id=001", log)
                self.assertNotIn("private-test-key", log)
                self.assertEqual(self.events()[0]["multiline"], "one\ntwo")
        self.assertEqual((sys.stdout, sys.stderr, logging.getLogger().level), before)

    def test_error_chain_is_saved_and_next_review_continues(self):
        model = SimpleNamespace(usage_calls=[])

        def tag(row, *_):
            if row["review_id"] == "001":
                try:
                    exc = RuntimeError("quota exhausted private-test-key")
                    exc.status_code = 429
                    exc.request_id = "req-123"
                    raise exc
                except RuntimeError as exc:
                    raise ValueError("provider wrapper") from exc
            return {"review_id": "002", "status": "ok", "insights": [], "rejected": []}

        with patch("review_tagger.__main__.create_model", return_value=model), patch("review_tagger.retry.tag_review", side_effect=tag):
            self.assertEqual(self.invoke(), 1)
        records = [json.loads(line) for line in (self.output / "reviews.jsonl").read_text().splitlines()]
        self.assertEqual([row["status"] for row in records], ["error", "ok"])
        cause = records[0]["error"]["causes"][1]
        self.assertEqual(cause["status_code"], 429)
        self.assertEqual(cause["request_id"], "req-123")
        self.assertIn("quota exhausted", cause["message"])
        self.assertIn("Traceback", records[0]["error"]["traceback"])
        self.assertEqual(self.events()[-1]["exit_code"], 1)
        for path in self.output.iterdir():
            self.assertNotIn("private-test-key", path.read_text())

    def test_raw_response_survives_real_parser_failure(self):
        model = UsageTrackingModel(model_id="qwen-test", api_key="private-test-key")
        model._client = Mock()
        response = ChatCompletion.model_validate({
            "id": "resp-123", "object": "chat.completion", "created": 0, "model": "qwen-test",
            "choices": [{"index": 0, "finish_reason": "length",
                         "message": {"role": "assistant", "content": '{"wrong": []}',
                                     "reasoning_content": "raw reasoning"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
        })
        model._client.chat.completions.create.return_value = response
        with patch("review_tagger.__main__.create_model", return_value=model):
            self.assertEqual(self.invoke("--limit", "1"), 1)
        events = self.events()
        api = next(e for e in events if e["event"] == "api_response")
        self.assertEqual(api["review_id"], "001")
        self.assertEqual(api["response"]["choices"][0]["finish_reason"], "length")
        self.assertEqual(api["response"]["choices"][0]["message"]["content"], '{"wrong": []}')
        self.assertTrue(any(e["event"] == "review_failed" for e in events))
        record = json.loads((self.output / "reviews.jsonl").read_text())
        self.assertEqual(record["usage"]["total_tokens"], 12)

    def test_api_failure_records_service_reason(self):
        model = UsageTrackingModel(model_id="qwen-test", api_key="private-test-key")
        model._client = Mock()
        exc = RuntimeError("service unavailable private-test-key")
        exc.status_code = 503
        exc.body = {"error": {"message": "upstream timeout"}}
        model._client.chat.completions.create.side_effect = exc
        with patch("review_tagger.__main__.create_model", return_value=model):
            self.assertEqual(self.invoke("--limit", "1"), 1)
        event = next(e for e in self.events() if e["event"] == "api_error")
        self.assertEqual(event["error"]["causes"][0]["status_code"], 503)
        self.assertIn("upstream timeout", json.dumps(event))
        self.assertNotIn("private-test-key", json.dumps(event))

    def test_rejected_extractions_explain_schema_and_grounding_failures(self):
        def extraction(attributes, start, end):
            return SimpleNamespace(extraction_class="review_insight", extraction_text="Thin",
                                   attributes=attributes,
                                   char_interval=SimpleNamespace(start_pos=start, end_pos=end))
        result = SimpleNamespace(extractions=[
            extraction({"detail": "太薄", "evidence_type": "实际体验", "topic": "invalid"}, 0, 4),
            extraction({"detail": "太薄", "evidence_type": "实际体验"}, 1, 5),
        ])
        with patch("review_tagger.__main__.create_model", return_value=SimpleNamespace(usage_calls=[])), patch("review_tagger.pipeline.lx.extract", return_value=result):
            self.assertEqual(self.invoke("--limit", "1"), 1)
        rejected = [e for e in self.events() if e["event"] == "extraction_rejected"]
        self.assertEqual([e["reason"] for e in rejected], ["invalid_schema", "not_exactly_grounded"])
        self.assertIn("attributes.topic", rejected[0]["details"])
        self.assertEqual(rejected[1]["matched_text"], "hin\n")
        self.assertEqual(rejected[1]["char_interval"], {"start_pos": 1, "end_pos": 5})
        self.assertEqual(json.loads((self.output / "reviews.jsonl").read_text())["status"], "needs_review")

    def test_interruption_and_startup_failure_are_logged(self):
        with patch("review_tagger.__main__.create_model", return_value=SimpleNamespace(usage_calls=[])), patch("review_tagger.retry.tag_review", side_effect=KeyboardInterrupt):
            self.assertEqual(self.invoke(), 130)
        self.assertEqual(self.events()[-1]["event"], "run_interrupted")
        self.assertEqual(self.events()[-1]["review_id"], "001")
        self.output = self.folder / "startup"
        with patch("review_tagger.__main__.create_model", side_effect=RuntimeError("model init failed")):
            self.assertEqual(self.invoke(), 2)
        self.assertEqual(self.events()[-1]["event"], "run_failed")
        self.assertIn("model init failed", (self.output / "run.log").read_text())

    def test_input_failure_and_dry_run_and_no_overwrite(self):
        self.input.write_text("wrong,columns\na,b\n")
        self.assertEqual(self.invoke(), 2)
        self.assertTrue(any(e["event"] == "validation_failed" for e in self.events()))
        original = (self.output / "run.log").read_bytes()
        self.assertEqual(self.invoke(), 2)
        self.assertEqual((self.output / "run.log").read_bytes(), original)
        self.output = self.folder / "dry"
        self.input.write_text("review_id,title,content\n001,Thin,too thin\n")
        with patch("review_tagger.__main__.create_model") as model:
            self.assertEqual(self.invoke("--dry-run"), 0)
            model.assert_not_called()
        self.assertEqual(self.events()[-1]["exit_code"], 0)


if __name__ == "__main__":
    unittest.main()
