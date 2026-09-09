import csv
import json
from pathlib import Path

import langextract as lx

from .schema import ReviewInsight


def read_reviews(path: Path):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        header = reader.fieldnames or []
        if len(header) != len(set(header)):
            raise ValueError("CSV 存在重复列名")
        if not {"review_id", "title", "content"} <= set(header):
            raise ValueError("CSV 必须包含 review_id、title、content")
        rows = list(reader)
    seen = set()
    for index, row in enumerate(rows, 2):
        if None in row or any(value is None for value in row.values()):
            raise ValueError(f"CSV 第 {index} 条记录列数不一致")
        if not row["review_id"].strip() or row["review_id"] in seen:
            raise ValueError(f"CSV 第 {index} 条记录 review_id 为空或重复")
        seen.add(row["review_id"])
    return rows


def load_examples(path: Path):
    examples = []
    for item in json.loads(path.read_text(encoding="utf-8")):
        extractions = []
        for raw in item["extractions"]:
            insight = ReviewInsight.model_validate(raw)
            if insight.extraction_text not in item["text"]:
                raise ValueError("示例原话无法定位")
            extractions.append(lx.data.Extraction(**insight.model_dump(exclude_none=True)))
        examples.append(lx.data.ExampleData(text=item["text"], extractions=extractions))
    if not examples:
        raise ValueError("至少需要一个 few-shot 示例")
    return examples


def validate_extractions(text, extractions):
    accepted, rejected, seen = [], [], set()
    for extraction in extractions or []:
        raw = dict(extraction_class=extraction.extraction_class,
                   extraction_text=extraction.extraction_text,
                   attributes=extraction.attributes or {})
        try:
            insight = ReviewInsight.model_validate(raw)
        except ValueError as exc:
            rejected.append({"reason": "invalid_schema", "extraction": raw,
                             "details": str(exc)})
            continue
        interval = extraction.char_interval
        if (interval is None or interval.start_pos is None or interval.end_pos is None
                or not 0 <= interval.start_pos < interval.end_pos <= len(text)
                or text[interval.start_pos:interval.end_pos] != insight.extraction_text):
            start = getattr(interval, "start_pos", None)
            end = getattr(interval, "end_pos", None)
            rejected.append({"reason": "not_exactly_grounded", "extraction": raw,
                             "char_interval": {"start_pos": start, "end_pos": end},
                             "text_length": len(text),
                             "matched_text": text[start:end] if isinstance(start, int) and isinstance(end, int) else None,
                             "details": "原话位置缺失、越界，或对应原文与 extraction_text 不逐字相同"})
            continue
        item = insight.model_dump(exclude_none=True)
        item["char_interval"] = {"start_pos": interval.start_pos, "end_pos": interval.end_pos}
        key = json.dumps(item, ensure_ascii=False, sort_keys=True)
        if key not in seen:
            seen.add(key)
            accepted.append(item)
    return accepted, rejected


def tag_review(row, model, prompt, examples):
    text = row["title"] + "\n" + row["content"]
    # Neither stars, old tags nor product marketing enter the extraction input.
    record = {"review_id": row["review_id"], "metadata": row,
              "text": text, "insights": [], "rejected": []}
    if not text.strip():
        record["status"] = "empty"
        return record
    result = lx.extract(
        text_or_documents=text, prompt_description=prompt, examples=examples,
        model=model, use_schema_constraints=False, fence_output=False,
        resolver_params={"suppress_parse_errors": False},
        max_char_buffer=max(1000, len(text) + 1), max_workers=1,
        extraction_passes=1, show_progress=False,
    )
    record["insights"], record["rejected"] = validate_extractions(text, result.extractions)
    record["status"] = ("needs_review" if record["rejected"] else
                        "ok" if record["insights"] else "empty_result")
    return record


def write_tagged_csv(path, rows, records):
    """Preserve the entire input; attach validated insights by review ID."""
    fields = list(rows[0])
    if "all_tags" in fields:
        raise ValueError("输入已包含 all_tags 列，无法在保留原列的同时追加同名列")
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields + ["all_tags"])
        writer.writeheader()
        for row in rows:
            record = records.get(row["review_id"])
            tags = ""
            if record and record["status"] != "error":
                tags = json.dumps(record["insights"], ensure_ascii=False)
            writer.writerow({**row, "all_tags": tags})
    temporary.replace(path)
