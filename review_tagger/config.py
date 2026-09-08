"""只读取模型配置，不加载其余服务的凭据。"""
import configparser
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True)
class ModelSettings:
    model_name: str
    base_url: str = field(repr=False)
    api_key: str = field(repr=False)


def load_settings(path: Path) -> ModelSettings:
    config = configparser.ConfigParser(interpolation=None)
    with path.open(encoding="utf-8-sig") as stream:
        config.read_file(stream)
    def required(section, key):
        value = config.get(section, key, fallback="").strip()
        if not value:
            raise ValueError(f"缺少配置 [{section}] {key}")
        return value
    settings = ModelSettings(
        required("model", "model_name"),
        required("model", "model_base_url"),
        required("openai", "openai_api_key"),
    )
    url = urlsplit(settings.base_url)
    if url.scheme not in {"http", "https"} or not url.netloc:
        raise ValueError("model_base_url 必须是 OpenAI 兼容 API 基础 URL")
    return settings
