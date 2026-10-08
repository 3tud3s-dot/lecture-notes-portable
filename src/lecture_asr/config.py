"""Local configuration only: importing this module performs no I/O."""

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values

# Configuration defaults do not establish API availability.
DEFAULT_BASE_URL = "https://api.siliconflow.cn/v1"
DEFAULT_ASR_MODEL = "Qwen/Qwen3-ASR-1.7B"


@dataclass(frozen=True)
class Settings:
    api_key: str | None = field(default=None, repr=False)
    base_url: str = DEFAULT_BASE_URL
    asr_model: str = DEFAULT_ASR_MODEL

    @property
    def transcription_endpoint(self) -> str:
        return self.base_url.rstrip("/") + "/audio/transcriptions"


def load_settings(env_file: Path | None = None) -> Settings:
    """Read the root .env in this source/editable project; environment wins.

    An absent default .env is allowed. An explicitly requested missing file is
    an error. No parent-directory search, environment mutation or interpolation.
    """
    path = Path(__file__).resolve().parents[2] / ".env" if env_file is None else env_file
    values: dict[str, str | None] = {}
    if env_file is not None or path.exists():
        with path.open(encoding="utf-8-sig") as stream:
            values = dotenv_values(stream=stream, interpolate=False)
    def read(name: str) -> str:
        return (os.environ.get(name, values.get(name)) or "").strip()

    return Settings(
        api_key=read("SILICONFLOW_API_KEY") or None,
        base_url=read("SILICONFLOW_BASE_URL").rstrip("/") or DEFAULT_BASE_URL,
        asr_model=read("SILICONFLOW_ASR_MODEL") or DEFAULT_ASR_MODEL,
    )
