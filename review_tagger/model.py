"""显式使用 OpenAI provider 连接 Qwen 的兼容端点。"""
from langextract.providers.openai import OpenAILanguageModel
from langextract.core import exceptions, types as core_types

from .config import ModelSettings
from .run_logging import error_details
import time


class UsageTrackingModel(OpenAILanguageModel):
    """Track responses before LangExtract discards their usage metadata (1.6.0)."""

    def __init__(self, enable_thinking=None, **kwargs):
        super().__init__(**kwargs)
        self.usage_calls = []
        self.enable_thinking = enable_thinking
        self.run_log = None

    def _process_single_prompt(self, prompt, config):
        params = self._build_chat_completions_params(prompt, config)
        if self.enable_thinking is not None:
            params["extra_body"] = {"enable_thinking": self.enable_thinking}
        started = time.monotonic()
        if self.run_log:
            self.run_log.event("api_request", parameters=params)
        try:
            response = self._client.chat.completions.create(**params)
        except Exception as exc:
            details = error_details(exc)
            if self.run_log:
                details = self.run_log.clean(details)
                self.run_log.event("api_error", error=details,
                                   duration_seconds=round(time.monotonic() - started, 3))
            self.usage_calls.append({"status": "error", "usage": None,
                                     "error_type": type(exc).__name__, "error": details})
            raise exceptions.InferenceRuntimeError(
                "Qwen API request failed", original=exc
            ) from exc
        # Persist the full SDK response before downstream parsing can fail.
        if self.run_log:
            self.run_log.event("api_response", response=response.model_dump(mode="json"),
                               duration_seconds=round(time.monotonic() - started, 3))
        usage = response.usage
        self.usage_calls.append({
            "status": "received", "response_id": response.id,
            "usage": usage.model_dump(exclude_none=True) if usage else None,
        })
        return core_types.ScoredOutput(score=1.0, output=response.choices[0].message.content)


def create_model(settings: ModelSettings, *, enable_thinking=None):
    model = UsageTrackingModel(
        model_id=settings.model_name,
        api_key=settings.api_key,
        base_url=settings.base_url,
        temperature=0,
        max_workers=1,
        enable_thinking=enable_thinking,
    )
    # LangExtract 1.6 does not forward timeout/retries to the SDK constructor.
    model._client = model._client.with_options(timeout=90, max_retries=2)
    return model
