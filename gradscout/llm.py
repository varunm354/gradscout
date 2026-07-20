"""Bounded, optional LLM job-analysis agent.

The deterministic monitor works with no API key. The provider sits behind a tiny
interface; when unavailable the agent is simply disabled. The agent runs ONLY on
new, relevant, ambiguous jobs (deterministic status == review), never across the
whole feed, and its output is validated with Pydantic. Any failure (unavailable,
timeout, invalid output) yields None so deterministic classification is retained.

Guardrails: the agent cannot control discovery, alter source fields, delete jobs,
send notifications, or write arbitrary DB values. It only returns a validated
AgentAnalysis that the resolver then reconciles with the deterministic result
(and hard deterministic rules always win).
"""

from __future__ import annotations

import json
import logging
import os
from typing import TYPE_CHECKING, Protocol

from gradscout.models import AgentAnalysis, Job

if TYPE_CHECKING:
    from gradscout.analyze import DeterministicAnalysis

logger = logging.getLogger("gradscout.llm")

DEFAULT_TIMEOUT = 20.0


class LLMUnavailable(RuntimeError):
    pass


class LLMProvider(Protocol):
    @property
    def available(self) -> bool: ...

    def complete(self, prompt: str, *, timeout: float) -> str: ...


class NullProvider:
    """Used when no API key is configured. The agent stays disabled."""

    available = False

    def complete(self, prompt: str, *, timeout: float) -> str:
        raise LLMUnavailable("no LLM provider configured")


class OpenAIProvider:
    def __init__(self, api_key: str, model: str = "gpt-4o-mini"):
        self._api_key = api_key
        self.model = model

    @property
    def available(self) -> bool:
        return bool(self._api_key)

    def complete(self, prompt: str, *, timeout: float) -> str:
        import openai  # imported lazily so the dep is optional

        client = openai.OpenAI(api_key=self._api_key, timeout=timeout)
        resp = client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0,
        )
        return resp.choices[0].message.content or ""


def provider_from_env() -> LLMProvider:
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        return NullProvider()
    model = os.environ.get("GRADSCOUT_LLM_MODEL", "gpt-4o-mini").strip() or "gpt-4o-mini"
    return OpenAIProvider(key, model)


def build_prompt(job: Job, det: DeterministicAnalysis) -> str:
    """Construct the analysis prompt. The agent is told the deterministic finding
    and asked ONLY to help resolve ambiguity, returning strict JSON."""
    schema = (
        '{"eligibility_status": "eligible|review|ineligible", "evidence": [..], '
        '"role_family": "backend|ai|data|product|other", '
        '"recommended_resume": "backend|ai|data|null", '
        '"resume_confidence": "high|medium|low|null", '
        '"priority_recommendation": "p1|p2|p3|review|ineligible", '
        '"uncertainty_reasons": [..], "requires_human_review": true|false, '
        '"deterministic_disagreement": true|false, "disagreement_reason": "..|null"}'
    )
    return (
        "You are GradScout's job-eligibility assistant for a candidate completing a "
        "BACHELOR'S degree in 2027 seeking full-time new-grad or eligible early-career "
        "technical roles. You cannot decide whether a listing exists; only classify the "
        "provided text. Never invent requirements.\n\n"
        f"Deterministic finding: {det.status.value} (reasons: {det.reasons}).\n\n"
        f"TITLE: {job.title}\nCOMPANY: {job.company}\n"
        f"DESCRIPTION:\n{(job.description_text or '')[:6000]}\n\n"
        f"Return ONLY JSON matching: {schema}"
    )


class JobAnalysisAgent:
    def __init__(
        self,
        provider: LLMProvider | None = None,
        *,
        enabled: bool = True,
        timeout: float = DEFAULT_TIMEOUT,
    ):
        self.provider: LLMProvider = provider or NullProvider()
        self.timeout = timeout
        self.enabled = enabled and self.provider.available

    def should_analyze(self, job: Job, det: DeterministicAnalysis) -> bool:
        """Cheap prefilter: only enabled agent + relevant + ambiguous (review) +
        not hard-ineligible jobs qualify. Clearly eligible/ineligible are skipped."""
        return (
            self.enabled
            and det.status.value == "review"
            and det.relevant
            and not det.hard_ineligible
        )

    def analyze(self, job: Job, det: DeterministicAnalysis) -> AgentAnalysis | None:
        if not self.should_analyze(job, det):
            return None
        prompt = build_prompt(job, det)
        try:
            raw = self.provider.complete(prompt, timeout=self.timeout)
        except Exception as exc:  # unavailable / timeout / transport
            logger.warning(
                "llm call failed",
                extra={"fields": {"company": job.company, "error": repr(exc)}},
            )
            return None
        try:
            data = json.loads(raw)
            return AgentAnalysis.model_validate(data)
        except Exception as exc:  # invalid / unparseable structured output
            logger.warning(
                "llm output invalid",
                extra={"fields": {"company": job.company, "error": repr(exc)}},
            )
            return None
