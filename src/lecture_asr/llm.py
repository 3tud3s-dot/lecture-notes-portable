"""V1 LLM provider preparation. Doctor is local-only; no Cleaner or automatic calls."""
import argparse
import json
import math
import os
from dataclasses import dataclass, field
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from urllib.parse import urlsplit

from . import config as shared_config
from .evidence import redact

DEFAULT_LLM_MODEL = 'Qwen/Qwen3.5-35B-A3B'  # Candidate, not frozen or runtime verified.
DEFAULT_LLM_TEMPERATURE = 0.1


class LLMConfigError(ValueError):
    """Messages must contain field names only, never invalid setting values."""


class LLMClientError(RuntimeError):
    def __init__(self, code, http_status=None):
        self.code = code
        self.http_status = http_status
        super().__init__(code)


@dataclass(frozen=True)
class LLMConfig:
    api_key: str | None = field(default=None, repr=False)
    base_url: str = field(default=shared_config.DEFAULT_BASE_URL, repr=False)
    model: str = field(default=DEFAULT_LLM_MODEL, repr=False)
    temperature: float = DEFAULT_LLM_TEMPERATURE
    provider: str = field(default='siliconflow', init=False)

    def __post_init__(self):
        try:
            url = urlsplit(self.base_url)
            valid_url = (url.scheme == 'https' and url.hostname and not url.username
                         and not url.password and not url.query and not url.fragment)
        except (ValueError, TypeError):
            valid_url = False
        if not valid_url:
            raise LLMConfigError('Invalid SILICONFLOW_BASE_URL.')
        if not isinstance(self.model, str) or not self.model.strip():
            raise LLMConfigError('Missing SILICONFLOW_LLM_MODEL.')
        if (isinstance(self.temperature, bool) or not isinstance(self.temperature, (int, float))
                or not math.isfinite(self.temperature) or not 0 <= self.temperature <= 2):
            raise LLMConfigError('Invalid SILICONFLOW_LLM_TEMPERATURE; expected finite 0..2.')

    @property
    def chat_endpoint(self):
        return self.base_url.rstrip('/') + '/chat/completions'

    def safe_report(self):
        # Whitelist first, then reuse the V0 redactor for accidentally contaminated options.
        return redact({
            'llm_provider':self.provider,
            'llm_sdk':'openai',
            'llm_api_key_status':'configured' if self.api_key else 'missing',
            'llm_base_url':self.base_url,
            'llm_chat_endpoint':self.chat_endpoint,
            'llm_model':self.model,
            'llm_model_status':'candidate / not frozen',
            'llm_temperature':self.temperature,
            'llm_runtime_verified':False,
        }, self.api_key or '')


def load_llm_config(env_file: Path | None = None):
    """Extend the existing config: credentials/base URL come only from load_settings.

    Only the two new LLM options are read here, from the same project-root .env
    with the same UTF-8 BOM, environment precedence and no-interpolation policy.
    The frozen V0 Settings / load_settings implementation is untouched.
    """
    try:
        shared = shared_config.load_settings(env_file)
        path = Path(shared_config.__file__).resolve().parents[2]/'.env' if env_file is None else env_file
        values = {}
        if env_file is not None or path.exists():
            with path.open(encoding='utf-8-sig') as stream:
                values = shared_config.dotenv_values(stream=stream, interpolate=False)
    except (OSError, UnicodeError, ValueError):
        raise LLMConfigError('Cannot read project configuration.') from None

    def option(name, default):
        # Missing values use defaults; explicitly blank values are invalid.
        value = os.environ[name] if name in os.environ else values.get(name, default)
        return str(value).strip() if value is not None else ''

    model = option('SILICONFLOW_LLM_MODEL', DEFAULT_LLM_MODEL)
    try:
        temperature = float(option('SILICONFLOW_LLM_TEMPERATURE', str(DEFAULT_LLM_TEMPERATURE)))
    except ValueError:
        raise LLMConfigError('Invalid SILICONFLOW_LLM_TEMPERATURE.') from None
    return LLMConfig(api_key=shared.api_key, base_url=shared.base_url, model=model, temperature=temperature)


class SiliconFlowLLMClient:
    """One SDK boundary. Construction never sends a request; no prompt policy."""
    def __init__(self, config: LLMConfig, *, timeout=60.0):
        from openai import OpenAI
        if not config.api_key:
            raise LLMConfigError('Missing SILICONFLOW_API_KEY.')
        self.config = config
        try:
            self._sdk = OpenAI(api_key=config.api_key, base_url=config.base_url,
                               timeout=timeout, max_retries=0)
        except Exception:
            raise LLMClientError('sdk_initialization_failed') from None

    def complete(self, messages, *, max_tokens, enable_thinking=None, response_format=None):
        """Explicit caller-owned messages only. No transcript/PDF loading or text edits.

        Not exposed by the CLI in Foundation. Tested with mocks only; future use
        requires the next stage's separately authorized request.
        """
        from openai import APIStatusError
        if type(max_tokens) is not int or max_tokens <= 0:
            raise ValueError('max_tokens must be a positive integer.')
        options = {}
        if enable_thinking is not None:
            if type(enable_thinking) is not bool:
                raise ValueError('enable_thinking must be boolean.')
            options['extra_body'] = {'enable_thinking': enable_thinking}
        if response_format is not None:
            if response_format != {'type': 'json_object'}:
                raise ValueError('Only json_object response format is supported.')
            options['response_format'] = response_format
        try:
            return self._sdk.chat.completions.create(
                model=self.config.model, temperature=self.config.temperature,
                messages=messages, max_tokens=max_tokens, stream=False, **options)
        except APIStatusError as error:
            # Never expose SDK exception strings, headers, URLs or response bodies.
            raise LLMClientError('api_status_error', error.status_code) from None
        except Exception:
            raise LLMClientError('llm_request_failed') from None

    def close(self):
        self._sdk.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description='Local LLM configuration doctor; never sends requests.')
    parser.add_argument('command', choices=['doctor'])
    parser.add_argument('--env-file', type=Path)
    parser.add_argument('--require-key', action='store_true')
    args = parser.parse_args(argv)
    try:
        config = load_llm_config(args.env_file)
        report = config.safe_report()
        try:
            report['llm_sdk_version'] = version('openai')
        except PackageNotFoundError:
            report['llm_sdk_version'] = None
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 2 if (args.require_key and not config.api_key) or report['llm_sdk_version'] is None else 0
    except LLMConfigError as error:
        print(json.dumps({'llm_config_status':'invalid', 'error':str(error),
                          'llm_runtime_verified':False}, ensure_ascii=False))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
