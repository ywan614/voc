from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from review_tagger.config import load_settings
from review_tagger.pipeline import load_examples, read_reviews, tag_review, validate_extractions
from review_tagger.schema import InsightAttributes

ROOT = Path(__file__).resolve().parent.parent


class TaggingTests(unittest.TestCase):
    def test_schema_boundaries(self):
        for extra in ({"topic": "质量"}, {"sentiment": "负面"},
                      {"behavior": "复购"}, {"behavior_reason": "便宜"},
                      {"scene": "送礼"}, {"priority": "P1"}):
            with self.assertRaises(ValidationError):
                InsightAttributes(detail="测试", evidence_type="实际体验", **extra)

    def test_exact_grounding_and_dedup(self):
        def extraction(quote, start, end, topic="面料"):
            return SimpleNamespace(extraction_class="review_insight", extraction_text=quote,
                attributes={"detail": "太薄", "evidence_type": "实际体验", "topic": topic},
                char_interval=SimpleNamespace(start_pos=start, end_pos=end))
        a = extraction("thin", 0, 4)
        accepted, rejected = validate_extractions("thin", [a, a,
            extraction("thin", 0, 4, "设计与功能"), extraction("Thin", 0, 4),
            extraction("thin", -1, 4), extraction("thin", None, None)])
        self.assertEqual(len(accepted), 2)
        self.assertEqual(len(rejected), 3)

    def test_examples_and_dataset(self):
        rows = read_reviews(ROOT / "data/sample.csv")
        sources = {r["review_id"]: r["title"] + "\n" + r["content"] for r in rows}
        import json
        for example in json.loads((ROOT / "review_tagger/examples.json").read_text()):
            for extraction in example["extractions"]:
                self.assertIn(extraction["extraction_text"], sources[example["source_review_id"]])
        self.assertEqual(len(load_examples(ROOT / "review_tagger/examples.json")), 7)

    def test_no_metadata_in_model_input(self):
        row = {"review_id": "001", "title": "Thin", "content": "too thin",
               "rating": "5", "tags": "old", "product_name": "marketing"}
        with patch("review_tagger.pipeline.lx.extract", return_value=SimpleNamespace(extractions=[])) as extract:
            result = tag_review(row, object(), "rules", [])
        self.assertEqual(extract.call_args.kwargs["text_or_documents"], "Thin\ntoo thin")
        self.assertFalse(extract.call_args.kwargs["resolver_params"]["suppress_parse_errors"])
        self.assertEqual(result["metadata"]["review_id"], "001")
        self.assertEqual(result["status"], "ok")

    def test_config_percent_and_redaction(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "config.ini"
            path.write_text("[model]\nmodel_name=qwen-test\nmodel_base_url=https://example.com/v1\n[openai]\nopenai_api_key=secret%value\n")
            settings = load_settings(path)
            self.assertEqual(settings.api_key, "secret%value")
            self.assertNotIn("secret", repr(settings))

    def test_malformed_csv(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "input.csv"
            for data in ("review_id,title,content\n1,a,b,c\n",
                         "review_id,title,content\n1,a,b\n1,c,d\n"):
                path.write_text(data)
                with self.assertRaises(ValueError):
                    read_reviews(path)


if __name__ == "__main__":
    unittest.main()
