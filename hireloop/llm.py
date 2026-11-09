"""Thin Claude wrapper: one structured-output call, validated by Pydantic.

Every LLM step in hireloop returns a Pydantic model, so the rest of the code
never parses free text. If no API key is configured, callers fall back to the
deterministic (keyword/rule) path — the tool still works offline, just dumber.
"""
from __future__ import annotations

import os
from typing import TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class LLMUnavailable(RuntimeError):
    pass


class LLM:
    def __init__(self, model: str = "claude-opus-5-5", effort: str = "medium"):
        self.model = model
        self.effort = effort
        self._client = None

    @staticmethod
    def available() -> bool:
        return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))

    @property
    def client(self):
        if self._client is None:
            if not self.available():
                raise LLMUnavailable("Set ANTHROPIC_API_KEY to enable AI features.")
            import anthropic

            self._client = anthropic.Anthropic()
        return self._client

    def structured(self, system: str, prompt: str, schema: type[T], *, effort: str | None = None,
                   max_tokens: int = 16000) -> T:
        import anthropic

        try:
            resp = self.client.beta.messages.parse(
                model=self.model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": prompt}],
                output_format=schema,
                output_config={"effort": effort or self.effort},
                # If the request is declined by a safety classifier, retry on a
                # fallback model chosen by the API instead of failing.
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except anthropic.RateLimitError as e:
            raise LLMUnavailable(f"Rate limited by the Claude API: {e}") from e
        except anthropic.APIConnectionError as e:
            raise LLMUnavailable(f"Could not reach the Claude API: {e}") from e
        if resp.stop_reason == "refusal":
            raise LLMUnavailable("The model declined this request.")
        if resp.stop_reason == "max_tokens" or resp.parsed_output is None:
            raise LLMUnavailable("The model response was incomplete.")
        return resp.parsed_output


def from_config(cfg, *, purpose: str = "default") -> LLM:
    llm_cfg = cfg["llm"]
    effort = llm_cfg.get("classify_effort", "low") if purpose == "classify" else llm_cfg.get("effort", "medium")
    return LLM(model=llm_cfg.get("model", "claude-opus-5-5"), effort=effort)
