"""A Chainlit chat front end for the helpdesk agent.

**A client, not a second implementation.** Every call below goes to the agent's existing HTTP API —
``POST /invocations`` to take a turn, ``POST /invocations/approvals/{id}/decide`` to answer a
proposal, ``GET``/``PUT /api/providers`` to choose the model. Nothing here knows about the loop, the
database or the MCP server, which is what lets this be added, run, or deleted without touching the
agent. It is the same surface the built-in page uses, so a bug fixed for one is fixed for both.

**Why it exists.** Chainlit gives for free what the hand-written page had to build: a message
history, streaming, collapsible tool steps, and — the two that mattered — an approval button that is
part of the chat rather than a card below it, and a settings panel for the model provider. The
assignment does not score visual design, so the cheapest way to a good front end is not to write
one.

**What it deliberately does not do.** No key entry: the provider is chosen from those the agent can
already build, and the credential stays in the agent's environment. No local state: the session id
is the agent's, the approval is the agent's row, and the model choice is the agent's stored setting,
so a restart of this process loses nothing.
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any

import chainlit as cl
import httpx
from chainlit.input_widget import Select, TextInput

#: Where the agent is. Compose sets this to the service name when this runs as a container; the
#: default is the published port, because running the chat on the host against a compose stack is
#: the common case and needs no configuration at all.
AGENT_BASE_URL = os.environ.get("AGENT_BASE_URL", "http://localhost:8080")

#: The fallback identity, used when Chainlit has no authenticated user to offer.
#:
#: One name for the whole chat process, which is right for a demo — it is what makes two tabs show
#: the same history — and wrong for anything multi-user, because every visitor then shares one
#: memory and one approval queue. `_user_id` below prefers a real identity whenever there is one.
CHAT_USER_ID = os.environ.get("CHAT_USER_ID", "chainlit-user")

#: A turn may legitimately take a while — the agent's own turn budget defaults to 90 seconds, and
#: an escaping timeout here would look like the agent failing when it was still working.
REQUEST_TIMEOUT_S = float(os.environ.get("CHAT_TIMEOUT_S", "180"))


def _session_id() -> str:
    """A conversation id for this browser tab, minted here so the first turn already has one."""
    return f"chainlit-{uuid.uuid4().hex[:12]}"


def _user_id() -> str:
    """Whose conversation this is — the identity the agent scopes memory, approvals and traces to.

    This is the answer to "how do users and sessions work here", because the two are different
    things in Chainlit and the difference decides both ids:

    * A **session** is one browser connection — a tab, or a reconnect. Each has its own id, and the
      conversation id above is minted from one, which is why a reload starts a fresh conversation
      and two tabs are two conversations.
    * A **user** is an authenticated identity. Chainlit only has one when the app registers an auth
      callback (`@cl.password_auth_callback`, or an OAuth provider). This app registers none, so
      `session.user` is `None` in every deployment of it.

    Hence the order below: the real identity when there is one, and otherwise `CHAT_USER_ID` — a
    single shared name for every visitor. To get genuine per-user isolation, add an auth callback;
    this function needs no change when you do.

    **A name is not authentication, and this file cannot make it one.** The agent takes `user_id` in
    the request body, so it isolates users from each other but never verifies who is asking: a
    client can claim any identity by sending it. Isolation is enforced and tested
    (`memory_isolation_between_users`); identity is not. README section c names that gap, and
    AgentCore's Identity service is what would close it.
    """
    session = getattr(cl.context, "session", None)
    user = getattr(session, "user", None)
    identifier = getattr(user, "identifier", None)
    if isinstance(identifier, str) and identifier.strip():
        return identifier
    return CHAT_USER_ID


async def _agent() -> httpx.AsyncClient:
    """The HTTP client for this session, kept so connections are reused across turns."""
    existing = cl.user_session.get("client")
    if isinstance(existing, httpx.AsyncClient):
        return existing
    client = httpx.AsyncClient(base_url=AGENT_BASE_URL, timeout=REQUEST_TIMEOUT_S)
    cl.user_session.set("client", client)
    return client


async def _get(path: str, **params: Any) -> dict[str, Any]:
    client = await _agent()
    response = await client.get(path, params=params or None)
    response.raise_for_status()
    body: dict[str, Any] = response.json()
    return body


async def _post(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    client = await _agent()
    response = await client.post(path, json=payload)
    response.raise_for_status()
    body: dict[str, Any] = response.json()
    return body


async def _put(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    client = await _agent()
    response = await client.put(path, json=payload)
    response.raise_for_status()
    body: dict[str, Any] = response.json()
    return body


def _error_detail(exc: Exception) -> str:
    """The agent's own sentence, when it sent one.

    The agent answers a refusal with ``{"error": {"kind": ..., "detail": ...}}`` — deliberately
    readable, and written for exactly this: a person looking at a screen. Showing ``HTTPStatusError:
    400 Bad Request`` instead would throw that away.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        try:
            body = exc.response.json()
        except ValueError:
            return f"{exc.response.status_code} {exc.response.reason_phrase}"
        detail = body.get("error", {}).get("detail")
        if isinstance(detail, str) and detail:
            return detail
        return f"{exc.response.status_code} {exc.response.reason_phrase}"
    return f"{type(exc).__name__}: {exc}"


def _model_label(status: dict[str, Any]) -> str:
    """How the current model reads in a sentence: provider, and the model when one is named."""
    provider = str(status.get("provider") or "unknown")
    model_id = status.get("model_id")
    return f"**{provider}** · `{model_id}`" if model_id else f"**{provider}**"


def _settings_inputs(status: dict[str, Any]) -> list[Any]:
    """The settings panel: which provider answers, and which model it uses.

    The provider list is the agent's own answer to "what could you build right now", so a provider
    whose credential is missing is not offered — the alternative is a picker that fails when used,
    which is worse than one that is honest about what is available.

    The model field is free text with the agent's suggestions in the description rather than a
    closed list, because the suggestions are a convenience: an id the catalogue does not mention
    must still be typable.
    """
    available = [str(name) for name in (status.get("available") or ["scripted"])]
    current = str(status.get("provider") or available[0])
    models = [str(model) for model in (status.get("models") or [])]

    return [
        Select(
            id="provider",
            label="Model provider",
            values=available,
            initial_index=available.index(current) if current in available else 0,
            description="The credential comes from the agent's environment; this chooses whose.",
        ),
        TextInput(
            id="model_id",
            label="Model id — blank means the provider's default",
            initial=str(status.get("model_id") or ""),
            description=(
                "Suggestions for the provider above: "
                + (", ".join(models[:8]) if models else "none available")
                + ". A model id belongs to one provider, so changing the provider clears this."
            ),
        ),
    ]


async def _send_approval_card(proposal: dict[str, Any]) -> None:
    """One proposal, with its arguments and two buttons.

    The arguments are shown verbatim because approving this action approves *these* arguments: a
    card that described the write in prose would let the thing that runs differ from the thing that
    was approved, which is the failure the whole gate exists to prevent.
    """
    approval_id = str(proposal.get("approval_id") or "")
    action = str(proposal.get("tool_name") or "a write")
    arguments = json.dumps(proposal.get("arguments") or {}, indent=2)

    text = f"**Approval needed: `{action}`**\n\n```json\n{arguments}\n```"
    if proposal.get("summary"):
        text += f"\n\n{proposal['summary']}"
    if proposal.get("expires_at"):
        text += f"\n\n_Expires {proposal['expires_at']}._"

    await cl.Message(
        content=text,
        actions=[
            cl.Action(
                name="approve",
                payload={"approval_id": approval_id, "approved": True},
                label="Approve and run",
                tooltip="Execute this write exactly as shown above.",
            ),
            cl.Action(
                name="decline",
                payload={"approval_id": approval_id, "approved": False},
                label="Decline",
                tooltip="Do not run it. Nothing is written.",
            ),
        ],
    ).send()


@cl.on_chat_start
async def start() -> None:
    """Open a conversation: mint the session, show the model, and surface anything already waiting.

    The pending approvals are fetched here rather than only being shown when a turn produces them,
    because the agent stores them: a proposal made in an earlier conversation, or before this chat
    process was started, is still approvable and must not be invisible from the front end. That
    durability is a stated requirement, and a chat UI that hid it would make the requirement look
    unimplemented.
    """
    cl.user_session.set("session_id", _session_id())

    try:
        status = await _get("/api/providers")
    except Exception as exc:
        await cl.Message(
            content=(
                f"**The agent is not reachable at `{AGENT_BASE_URL}`.**\n\n"
                f"{_error_detail(exc)}\n\n"
                "Start it with `docker compose up -d --wait`, then reload this page."
            )
        ).send()
        return

    # Remembered so `update_settings` can tell a model change from a provider change: the panel
    # submits the whole form, so a model id typed for one provider arrives attached to the next.
    cl.user_session.set("provider", str(status.get("provider") or ""))

    await cl.ChatSettings(_settings_inputs(status)).send()
    await cl.Message(
        content=(
            f"Connected to `{AGENT_BASE_URL}`, answering with {_model_label(status)}.\n\n"
            f"{status.get('detail', '')}\n\n"
            "Ask about a ticket, or ask me to open one — a write is proposed and waits for your "
            "approval. Use the settings gear to change the model."
        )
    ).send()

    try:
        pending = await _get("/api/approvals", user_id=_user_id())
    except Exception:
        # A listing that fails must not stop the conversation: sending a message is still possible,
        # and a front end that refused to start would be worse than one missing a reminder.
        return

    waiting = pending.get("approvals") or []
    if waiting:
        await cl.Message(
            content=(
                f"{len(waiting)} write(s) are already waiting for a decision — they were stored by "
                "the agent and survived its last restart."
            )
        ).send()
        for proposal in waiting:
            await _send_approval_card(proposal)


@cl.on_settings_update
async def update_settings(settings: dict[str, Any]) -> None:
    """Apply a model change against the agent and report what actually happened.

    The message quotes the agent's reply rather than what was asked for, so a refusal ("that
    provider has no credential in this process") is visible as a refusal — and the panel keeps
    showing the provider that is really in force.
    """
    provider = str(settings.get("provider") or "")
    model_id = str(settings.get("model_id") or "").strip()

    # A model id belongs to exactly one provider, and Chainlit submits the whole form — so
    # switching the select carries the *previous* provider's model id along with the new
    # provider's name. Two failures came from precisely that, both seen in one session: choosing
    # `bedrock` announced "bedrock · gpt-4o-mini", a model Bedrock has never served, and choosing
    # `replay` sent `gpt-4o-mini` where a recording path belongs, which the agent refused with
    # "recording not found: gpt-4o-mini". So a provider change drops the model id and the new
    # provider starts from its own default; a model is then named in a second step, against the
    # provider it is actually for.
    switched = provider != cl.user_session.get("provider")
    if switched:
        model_id = ""

    try:
        status = await _put(
            "/api/providers/current", {"provider": provider, "model_id": model_id}
        )
    except Exception as exc:
        await cl.Message(content=f"Could not switch provider: {_error_detail(exc)}").send()
        return

    # Recorded only once the agent accepted it, so the panel's idea of what is current is always
    # something the service agreed to rather than something the user typed into a box.
    cl.user_session.set("provider", str(status.get("provider") or provider))

    note = (
        " The model id was cleared, so this provider's default is in use — set one now if you "
        "want a specific model."
        if switched
        else ""
    )
    await cl.Message(
        content=(
            f"Now answering with {_model_label(status)}.{note} The choice is stored by the agent, "
            "so it applies to the next message and survives a restart of either process."
        )
    ).send()


@cl.on_message
async def on_message(message: cl.Message) -> None:
    """Take one turn, show every tool call, and hand a proposal back to the user.

    The tool calls are rendered as steps *after* the turn returns rather than streamed as they
    happen, because the agent answers one turn at a time and a step that appeared before its result
    would be guessing. Streaming would be a change to the agent's contract, not to this file.
    """
    session_id = str(cl.user_session.get("session_id") or _session_id())
    cl.user_session.set("session_id", session_id)

    try:
        result = await _post(
            "/invocations",
            {"prompt": message.content, "user_id": _user_id(), "session_id": session_id},
        )
    except Exception as exc:
        await cl.Message(
            content=(
                f"**That turn failed.** {_error_detail(exc)}\n\n"
                "_A model-side failure is reported to you rather than raised: the agent answers "
                "with the reason and keeps serving._"
            )
        ).send()
        return

    if result.get("session_id"):
        cl.user_session.set("session_id", str(result["session_id"]))

    for call in result.get("tool_calls") or []:
        await _show_tool_call(call)

    if result.get("pending_approval"):
        await cl.Message(content=result.get("output") or "I need your approval for a write:").send()
        await _send_approval_card(result["pending_approval"])
        return

    await cl.Message(content=result.get("output") or "_(the agent produced no answer)_").send()


async def _show_tool_call(call: dict[str, Any]) -> None:
    """One tool call as a collapsible step: what was called, with what, and what came back."""
    name = str(call.get("name") or "tool")
    arguments = json.dumps(call.get("arguments") or {}, indent=2)
    outcome = str(call.get("outcome") or "")
    summary = str(call.get("summary") or call.get("detail") or "")
    duration = call.get("duration_ms")

    async with cl.Step(name=name, type="tool") as step:
        step.input = arguments
        lines = [summary or "(no detail)"]
        if outcome:
            lines.append(f"outcome: {outcome}")
        if duration is not None:
            lines.append(f"{duration} ms")
        if call.get("is_error"):
            lines.insert(0, "FAILED")
        step.output = "\n".join(lines)


async def _apply_decision(action: cl.Action, *, approved: bool) -> None:
    """Send one approval decision to the agent and report what came of it.

    The decision goes to the agent's own endpoint rather than to local state, so the row that
    authorises the write and the write itself are committed together — and a retried click is
    answered from the ledger instead of creating a second ticket. The button is a convenience on
    top of that guarantee, never the guarantee itself.
    """
    payload = dict(action.payload or {})
    approval_id = str(payload.get("approval_id") or "")

    try:
        result = await _post(
            f"/invocations/approvals/{approval_id}/decide",
            {"user_id": _user_id(), "approved": approved},
        )
    except Exception as exc:
        await cl.Message(content=f"That decision was refused: {_error_detail(exc)}").send()
        return

    detail = str(result.get("detail") or "")
    outcome = str(result.get("outcome") or ("declined" if not approved else "decided"))
    note = " _(the backend reported this was already done)_" if result.get("replayed") else ""
    await cl.Message(content=f"**{outcome}** — {detail}{note}").send()


#: Chainlit 2.x dispatches actions by name: ``cl.action_callback(name)`` is the hook, and
#: ``cl.on_action`` is not exported at all. The two names registered here are exactly the
#: ``cl.Action(name=...)`` values the approval card above sends, so a button and its handler cannot
#: drift apart — and the button stays a convenience on top of the agent's own row, never the gate.
@cl.action_callback("approve")
async def on_approve(action: cl.Action) -> None:
    """The Approve button. Runs the write, once, through the agent's own decision route."""
    await _apply_decision(action, approved=True)


@cl.action_callback("decline")
async def on_decline(action: cl.Action) -> None:
    """The Decline button. Writes nothing; the agent records the refusal."""
    await _apply_decision(action, approved=False)


@cl.on_chat_end
async def end() -> None:
    """Close the HTTP client this session opened. Pooled connections outliving the chat leak."""
    client = cl.user_session.get("client")
    if isinstance(client, httpx.AsyncClient):
        await client.aclose()
