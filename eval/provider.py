"""Which model the eval run uses, and what that needs.

The suite runs on `scripted` by default, and that is the right default: it needs no credential, it
costs nothing, and it is the only way a case asserting "the right tool was chosen" can be a
*regression gate* rather than a coin toss. But those cases then say nothing about a live model's
judgement — the gap README section e names — and the honest way to say something is to run the same
suite against a real model and publish what happens, including the cases that fail.

This module is the one place that decides three things:

* **Which provider.** `EVAL_PROVIDER`, defaulting to `scripted`. The name is checked against the
  providers the agent actually offers, because `EVAL_PROVIDER=opneai` should be a sentence at the
  start of a run rather than a stack that comes up on `scripted` and reports a green suite that
  never touched a model.
* **What it needs.** A live provider needs a credential, and it is read from the same places the
  agent reads its own: the shell first, then `.env`. Missing is refused *here*, with the variable
  named, rather than surfacing later as a 401 inside one case's payload.
* **What the agent process must be given.** Returned as an environment mapping the stack merges
  into its own, so the credential reaches the process under test the way a deployment would supply
  it — and it is never printed or written to the report.

One deliberate omission. This does not read `EVAL_BASE_URL` or `EVAL_TICKETING_URL`, which
`.env.example` documented for a while and no code read — the same defect class README section j
describes. They are deleted from the template rather than implemented here: the suite starts its own
stack on free ports and has never had a use for either.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Where the template's values live once a developer has copied it.
ENV_FILE = REPO_ROOT / ".env"

#: The variable that names the provider this run uses.
PROVIDER_ENV = "EVAL_PROVIDER"

#: What the suite runs on unless told otherwise.
DEFAULT_PROVIDER = "scripted"

#: Providers the agent offers, and the credential each needs to be buildable.
#:
#: `None` means "no single credential variable": `scripted` needs nothing, `replay` needs a
#: recording rather than a key, and `bedrock` takes its credentials from boto3's own chain (a
#: shared file, a profile, an instance role) rather than from one environment variable — so
#: demanding a key here would refuse a machine configured the normal way.
PROVIDERS: dict[str, str | None] = {
    "scripted": None,
    "replay": None,
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "openai_compatible": None,
    "bedrock": None,
}

#: Providers that answer without calling a model. The distinction is what the report prints, and
#: what a reader needs in order to know whether a number describes judgement or plumbing.
DETERMINISTIC_PROVIDERS = frozenset({"scripted", "replay"})


class ProviderError(RuntimeError):
    """The run cannot be configured as asked. Distinct from a case failing."""


@dataclass(frozen=True)
class EvalProvider:
    """The provider this run uses, and the environment the agent needs for it."""

    name: str
    #: Passed to the agent process. Never printed: it can carry a credential.
    env: Mapping[str, str] = field(default_factory=dict)

    @property
    def is_live(self) -> bool:
        """Whether a model is actually being called.

        The word the report uses, and the reason to run the suite twice: a deterministic run proves
        the orchestration, and only a live run says anything about judgement.
        """
        return self.name not in DETERMINISTIC_PROVIDERS

    def describe(self) -> str:
        """One line for the terminal and the report, with no credential in it."""
        kind = "a live model" if self.is_live else "no model (deterministic)"
        return f"{self.name} — {kind}"


def _resolve_environment(environment: Mapping[str, str] | None) -> dict[str, str]:
    """The shell's environment, with `.env` underneath it.

    Shell first, file second — the same precedence the agent's own settings use and the order
    `devtasks chat` applies: a value typed for one run is a decision about that run, and `.env` is
    standing configuration.

    Reading the file at all is what makes `EVAL_PROVIDER=openai`, and the key beside it, work in the
    place a developer already keeps both. `.env` is gitignored and is where `.env.example` says
    credentials go, so a suite that consulted only the process environment would contradict the
    template that documents it.
    """
    if environment is not None:
        return dict(environment)

    merged: dict[str, str] = {}
    if ENV_FILE.is_file():
        merged.update({key: value for key, value in dotenv_values(ENV_FILE).items() if value})
    merged.update({key: value for key, value in os.environ.items() if value})
    return merged


def resolve_provider(environment: Mapping[str, str] | None = None) -> EvalProvider:
    """The provider for this run, or an explanation of why it cannot be used.

    ``environment`` is a parameter so a test can drive every branch — a missing key, an unknown
    name, a provider that needs a base URL — without mutating the process environment, which the
    rest of the suite has to be careful about for a different reason.
    """
    env = _resolve_environment(environment)
    # Stripped *before* the fallback, not after. The obvious `(value or DEFAULT).strip()` is wrong
    # for the one input that matters here: `"   "` is truthy, so it survives the `or`, strips to an
    # empty string, and is then refused as an unknown provider name. A variable set to nothing is
    # "not configured" — which is how compose passes `${VAR:-}` — and has to behave that way.
    configured = (env.get(PROVIDER_ENV) or "").strip()
    name = configured or DEFAULT_PROVIDER

    if name not in PROVIDERS:
        raise ProviderError(
            f"{PROVIDER_ENV}={name!r} is not a provider this agent offers. "
            f"Choose one of: {', '.join(sorted(PROVIDERS))}"
        )

    agent_env: dict[str, str] = {"AGENT_MODEL_PROVIDER": name}

    credential_var = PROVIDERS[name]
    if credential_var is not None:
        credential = (env.get(credential_var) or "").strip()
        if not credential:
            raise ProviderError(
                f"{PROVIDER_ENV}={name} needs {credential_var}, which is in neither the shell nor "
                f"{ENV_FILE.name}. Add it to .env (the file is gitignored) or export it — or run "
                f"the suite with {PROVIDER_ENV}=scripted, which is what the committed results and "
                f"CI use."
            )
        agent_env[credential_var] = credential

    if name == "openai_compatible":
        # No key, but an address. Refused here rather than at the first model call, for the same
        # reason as a missing key: a case's failure should not be where the mistake is reported.
        base_url = (env.get("OPENAI_BASE_URL") or "").strip()
        if not base_url:
            raise ProviderError(
                f"{PROVIDER_ENV}=openai_compatible needs OPENAI_BASE_URL: the address of an "
                f"OpenAI-shaped endpoint (Ollama, vLLM, LM Studio, Groq, Together)."
            )
        agent_env["OPENAI_BASE_URL"] = base_url

    if name == "bedrock":
        region = (env.get("AWS_REGION") or "").strip()
        if not region:
            raise ProviderError(f"{PROVIDER_ENV}=bedrock needs AWS_REGION.")
        agent_env["AWS_REGION"] = region

    return EvalProvider(name=name, env=agent_env)
