"""Per-run console capture and structured diagnostics, flushed as events occur."""
import configparser
from datetime import datetime, timezone
import json
import logging
import re
import sys
import threading
import traceback
from urllib.parse import quote


def error_details(exc):
    chain, seen = [], set()
    current = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        item = {"type": type(current).__name__, "message": str(current)}
        for key in ("status_code", "request_id", "body"):
            value = getattr(current, key, None)
            if value is not None:
                item[key] = value
        chain.append(item)
        current = current.__cause__ or getattr(current, "original", None) or (
            None if current.__suppress_context__ else current.__context__)
    return {"type": type(exc).__name__, "message": str(exc), "causes": chain,
            "traceback": "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))}


class Redactor:
    def __init__(self, config_path):
        self.secrets = set()
        config = configparser.ConfigParser(interpolation=None)
        try:
            config.read(config_path, encoding="utf-8-sig")
        except (OSError, configparser.Error, UnicodeError):
            pass
        for section in config.sections():
            for key, value in config.items(section):
                if re.search(r"key|token|secret|password|credential", key, re.I) and value.strip():
                    self.add(value.strip())

    def add(self, value):
        self.secrets.update((value, quote(value, safe=""), json.dumps(value)[1:-1]))

    def __call__(self, text):
        for secret in sorted(self.secrets, key=len, reverse=True):
            text = text.replace(secret, "[REDACTED]")
        text = re.sub(r"(?i)(bearer\s+)[^\s\"'<>]+", r"\1[REDACTED]", text)
        text = re.sub(r"(https?://)[^/@\s]+:[^/@\s]+@", r"\1[REDACTED]@", text)
        return text


class Tee:
    def __init__(self, original, run, channel):
        self.original, self.run, self.channel = original, run, channel
        self.pending = ""

    def write(self, text):
        # Buffer fragmented print writes so a split credential is redacted as a unit.
        with self.run.lock:
            self.pending += text
            while "\n" in self.pending or "\r" in self.pending:
                match = re.search(r"[\r\n]", self.pending)
                end = match.end()
                self._emit(self.pending[:end])
                self.pending = self.pending[end:]
        return len(text)

    def _emit(self, text):
        safe = self.run.redact(text)
        self.original.write(safe)
        self.original.flush()
        prefix = f"{self.run.timestamp()} [{self.channel}] review_id={self.run.review_id or '-'} "
        self.run.console.write(prefix + safe + ("" if safe.endswith("\n") else "\n"))
        self.run.console.flush()

    def flush(self):
        with self.run.lock:
            if self.pending:
                self._emit(self.pending)
                self.pending = ""
            self.original.flush()

    def __getattr__(self, name):
        return getattr(self.original, name)


class RunLog:
    def __init__(self, output, config_path):
        self.output = output
        self.redact = Redactor(config_path)
        self.lock = threading.RLock()
        self.review_id = None

    @staticmethod
    def timestamp():
        return datetime.now(timezone.utc).isoformat()

    def clean(self, value):
        def walk(item):
            if isinstance(item, str):
                return self.redact(item)
            if isinstance(item, list):
                return [walk(value) for value in item]
            if isinstance(item, dict):
                return {key: walk(value) for key, value in item.items()}
            return item
        return walk(json.loads(json.dumps(value, ensure_ascii=False, default=str)))

    def event(self, event, **fields):
        with self.lock:
            record = {"timestamp": self.timestamp(), "event": event,
                      "review_id": self.review_id, **fields}
            self.events.write(json.dumps(self.clean(record), ensure_ascii=False) + "\n")
            self.events.flush()

    def __enter__(self):
        self.console = (self.output / "run.log").open("x", encoding="utf-8")
        try:
            self.events = (self.output / "events.jsonl").open("x", encoding="utf-8")
        except BaseException:
            self.console.close()
            raise
        self.stdout, self.stderr = sys.stdout, sys.stderr
        sys.stdout = Tee(self.stdout, self, "stdout")
        sys.stderr = Tee(self.stderr, self, "stderr")
        self.root = logging.getLogger()
        self.old_level = self.root.level
        self.old_handlers = self.root.handlers[:]
        self.handler = logging.StreamHandler(sys.stderr)
        self.handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        # Existing console handlers may retain the original stderr, bypassing
        # redaction and duplicating output. Restore them when the run finishes.
        self.root.handlers = [self.handler]
        self.root.setLevel(min(self.old_level, logging.INFO))
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc is not None:
                self.event("run_interrupted" if isinstance(exc, KeyboardInterrupt) else "run_failed",
                           error=error_details(exc))
                traceback.print_exception(exc_type, exc, tb)
            sys.stdout.flush()
            sys.stderr.flush()
        finally:
            self.root.handlers = self.old_handlers
            self.root.setLevel(self.old_level)
            sys.stdout, sys.stderr = self.stdout, self.stderr
            self.handler.close()
            self.events.close()
            self.console.close()
