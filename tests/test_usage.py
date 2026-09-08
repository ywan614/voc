from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from review_tagger.model import UsageTrackingModel
from review_tagger.usage import summarize_usage
from review_tagger.pipeline import tag_review, load_examples
from pathlib import Path


class UsageTests(unittest.TestCase):
    def model(self):
        model = UsageTrackingModel(model_id="qwen-test", api_key="test")
        model._client = Mock()
        return model

    def test_records_usage_before_downstream_parsing(self):
        model = self.model()
        usage = Mock()
        usage.model_dump.return_value = {"prompt_tokens": 10, "completion_tokens": 4,
                                        "total_tokens": 14, "completion_tokens_details": {"reasoning_tokens": 2}}
        model._client.chat.completions.create.return_value = SimpleNamespace(
            id="test-response", usage=usage,
            choices=[SimpleNamespace(message=SimpleNamespace(content="invalid json"))])
        model._process_single_prompt("test", {})
        model._process_single_prompt("test", {})
        self.assertEqual(summarize_usage(model.usage_calls)["total_tokens"], 28)
        self.assertEqual(model.usage_calls[0]["usage"]["completion_tokens_details"]["reasoning_tokens"], 2)

    def test_missing_usage_is_not_reported_as_complete(self):
        result = summarize_usage([{"usage": None}, {"usage": {
            "prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}}])
        self.assertFalse(result["usage_complete"])
        self.assertEqual(result["total_tokens"], 12)
        self.assertEqual(result["calls_without_complete_usage"], 1)

    def test_failed_call_remains_unknown(self):
        model = self.model()
        model._client.chat.completions.create.side_effect = RuntimeError("failure")
        with self.assertRaises(Exception):
            model._process_single_prompt("test", {})
        self.assertFalse(summarize_usage(model.usage_calls)["usage_complete"])

    def test_no_thinking_is_forwarded(self):
        model = self.model()
        model.enable_thinking = False
        model._client.chat.completions.create.return_value = SimpleNamespace(
            id="test", usage=None, choices=[SimpleNamespace(message=SimpleNamespace(content='{"extractions": []}'))])
        model._process_single_prompt("test", {})
        self.assertEqual(model._client.chat.completions.create.call_args.kwargs["extra_body"], {"enable_thinking": False})

    def test_real_langextract_parsing_preserves_usage_on_failure(self):
        model = self.model()
        usage = Mock()
        usage.model_dump.return_value = {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}
        model._client.chat.completions.create.return_value = SimpleNamespace(
            id="test", usage=usage, choices=[SimpleNamespace(message=SimpleNamespace(content='{"wrong": []}'))])
        examples = load_examples(Path(__file__).resolve().parent.parent / "review_tagger/examples.json")
        with self.assertRaises(Exception):
            tag_review({"review_id": "test", "title": "Thin", "content": "too thin"}, model, "Extract JSON", examples[:1])
        self.assertEqual(summarize_usage(model.usage_calls)["total_tokens"], 12)


if __name__ == "__main__":
    unittest.main()
