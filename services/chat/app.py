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

import hmac
import json
import logging
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

#: The demo accounts this chat accepts, as ``name:password`` pairs separated by commas —
#: ``CHAT_USERS="alice:correct-horse,bob:swordfish"``.
#:
#: **Empty by default, deliberately.** With no accounts the app registers no authentication callback
#: at all, so it behaves exactly as it did before login existed: every visitor is anonymous and
#: shares `CHAT_USER_ID`. Shipping a default account would mean shipping a default password, which
#: is the one thing a login screen must never do — and it would make this look like access control
#: when it is a demonstration of identity.
CHAT_USERS = os.environ.get("CHAT_USERS", "")

#: A turn may legitimately take a while — the agent's own turn budget defaults to 90 seconds, and
#: an escaping timeout here would look like the agent failing when it was still working.
REQUEST_TIMEOUT_S = float(os.environ.get("CHAT_TIMEOUT_S", "180"))

logger = logging.getLogger(__name__)


def _forget_the_agents_database() -> None:
    """Remove ``DATABASE_URL``, which this front end must not act on.

    Chainlit reads a set ``DATABASE_URL`` as "persist chats to Postgres", and the first time anybody
    logs in it imports its Postgres data layer to do it — a module whose very first line imports
    ``asyncpg``. This environment has neither that library nor a database, so the import raises
    ``ModuleNotFoundError`` and the login route answers **500** with a traceback about a driver
    nobody configured.

    That is reachable in ordinary use, not theoretically. This repository's ``.env`` sets
    ``DATABASE_URL`` for the *agent*, and Chainlit loads a ``.env`` from its **working directory** —
    ``load_dotenv(dotenv_path=os.path.join(os.getcwd(), env_file))``, with no search upwards — so
    starting the chat by hand from the repository root hands the agent's database URL to the chat
    front end, and login stops working for a reason with no visible connection to anything anybody
    did. Found exactly that way: the login screen rendered, and signing in returned 500.

    Removed here rather than only avoided by the command that starts the app, because the trap is
    one variable reaching the process and this is the one place every way in passes through:
    ``devtasks chat``, a server started by hand, and a container.

    Deleting it is the fix rather than a workaround, because the value means nothing here. The chat
    keeps no state of its own — the session id is the agent's, the approvals are the agent's rows,
    the model choice is the agent's stored setting — so there is nothing a data layer could store,
    and the only thing this variable can do is break login. Written as a named function so the
    reason travels with the line that does it.
    """
    if os.environ.pop("DATABASE_URL", None) is not None:
        logger.info(
            "ignored a DATABASE_URL in the environment: this front end keeps no state, and "
            "Chainlit would try to build a Postgres data layer for it"
        )


# Run at import, because Chainlit reads the variable lazily — on the first login — so clearing it
# here is early enough however the app is started, and late enough to have no effect on anything
# else in the process.
_forget_the_agents_database()


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
      callback — this one is `@cl.password_auth_callback`, registered above from `CHAT_USERS` — so
      with no accounts configured `session.user` is `None` and every visitor is anonymous.

    Hence the order below: the real identity when there is one, and otherwise `CHAT_USER_ID` — a
    single shared name for every visitor. That fallback is why setting `CHAT_USERS` is the whole
    difference between one shared memory for everybody and one per person.

    **A name is not authentication, and this file cannot make it one.** The agent takes `user_id` in
    the request body, so it isolates users from each other but never verifies who is asking: a
    client can claim any identity by sending it. Isolation is enforced and tested
    (`memory_isolation_between_users`); identity is not. The login above makes the name *chosen*
    rather than *guessed*, which is a real improvement and still not the same thing as verified —
    README section c names that gap, and AgentCore's Identity service is what would close it.
    """
    session = getattr(cl.context, "session", None)
    user = getattr(session, "user", None)
    identifier = getattr(user, "identifier", None)
    if isinstance(identifier, str) and identifier.strip():
        return identifier
    return CHAT_USER_ID


def _accounts() -> dict[str, str]:
    """The configured demo accounts, name to password.

    Parsed from ``CHAT_USERS``. A malformed entry is skipped rather than fatal: this is
    configuration, and refusing to start the chat because of a stray comma would be a worse failure
    than ignoring one. An entry needs a colon and a non-empty name; everything after the first colon
    is the password, so a password may itself contain one.
    """
    accounts: dict[str, str] = {}
    for entry in CHAT_USERS.split(","):
        name, separator, password = entry.partition(":")
        if separator and name.strip() and password:
            accounts[name.strip()] = password
    return accounts


async def _authenticate(username: str, password: str) -> cl.User | None:
    """Accept or refuse one login attempt.

    **What this is.** A real per-user identity, so the thing the agent scopes memory, approvals and
    conversations to is chosen by the person typing it rather than handed to everybody by the front
    end. Everything downstream of this needs no change: `_user_id` above reads
    ``session.user.identifier``, so logging in is what switches the agent from one shared identity
    to one per person.

    **What this is not.** Authentication, in any sense that would survive a security review. There
    is no user database, no password hashing — the comparison is against plaintext in the
    environment — no lockout, no password policy and no revocation. Chainlit does issue its own
    signed session cookie, so a login here is not trivially forgeable, but this exists to
    demonstrate the identity plumbing and should be replaced by `@cl.oauth_callback` (or Chainlit's
    own data layer) in anything real. `README.md` section c names identity as the gap AgentCore's
    Identity service would close, and this is the smallest thing that makes that gap visible.

    ``compare_digest`` rather than ``==`` because the one detail worth getting right even in a demo
    is not leaking a password one character at a time through timing — and it costs nothing to do.
    """
    expected = _accounts().get(username)
    if expected is None or not hmac.compare_digest(expected, password):
        return None
    return cl.User(identifier=username, display_name=username)


#: Registered only when there are accounts to check against, and by calling the decorator rather
#: than using it — its entire effect is this assignment, so there is no way to write
#: `@cl.password_auth_callback` conditionally. The condition matters: registering a callback that
#: can never return a user would show a login screen nobody can get past, and skipping it when
#: nothing is configured keeps the out-of-the-box behaviour exactly as it was.
if _accounts():
    cl.password_auth_callback(_authenticate)


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


#: The picker's "start a new one" row. A label rather than an id, because it stands for nothing the
#: agent knows about yet — the id for a new conversation is minted when it is chosen.
NEW_CONVERSATION = "New conversation"


def _conversation_options(sessions: list[dict[str, Any]]) -> tuple[list[str], dict[str, str]]:
    """The conversation picker's choices, and the conversation each one stands for.

    The mapping is not a convenience: Chainlit hands back the *label* somebody picked and never an
    index, so a label has to be unique and has to be reversible. Two conversations can honestly open
    with the same words — "find tickets about the printer" twice is a normal week — so a repeated
    label gets its session id's tail appended rather than being merged with the other one, which
    would make one of them unreachable from the picker.

    The message count is in the label for the same reason it is in the listing: it is how somebody
    tells the conversation they were working in from one they opened and abandoned.
    """
    values = [NEW_CONVERSATION]
    mapping: dict[str, str] = {}
    seen: set[str] = set()

    for session in sessions:
        session_id = str(session.get("session_id") or "")
        if not session_id:
            continue
        preview = str(session.get("preview") or "(no messages)")
        count = session.get("messages") or 0
        label = f"{preview} · {count} message(s)"
        if label in seen:
            label = f"{label} · {session_id[-4:]}"
        seen.add(label)
        values.append(label)
        mapping[label] = session_id

    return values, mapping


def _settings_inputs(status: dict[str, Any], conversations: list[str]) -> list[Any]:
    """The settings panel: which conversation, which provider, and which model.

    Three unrelated-looking controls in one panel because Chainlit has exactly one panel — and the
    conversation belongs in it rather than on a message with buttons, because it is a *selection*
    rather than a decision: nothing is written, nothing needs approving, and picking differently is
    a normal thing to do immediately afterwards.

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
            id="conversation",
            label="Conversation",
            values=conversations,
            initial_index=0,
            description=(
                "Pick one of your earlier conversations to redraw it and carry on, or start a new "
                "one. Only conversations belonging to you are listed."
            ),
        ),
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

    # This user's earlier conversations, for the picker. A failure here must not stop the chat —
    # sending a message is still possible, and an empty picker is a small loss beside a front end
    # that refuses to open at all.
    try:
        listed = await _get("/api/sessions", user_id=_user_id())
        sessions = [item for item in (listed.get("sessions") or []) if isinstance(item, dict)]
    except Exception:
        sessions = []

    conversations, mapping = _conversation_options(sessions)
    cl.user_session.set("conversations", mapping)
    cl.user_session.set("conversation", NEW_CONVERSATION)

    await cl.ChatSettings(_settings_inputs(status, conversations)).send()
    await cl.Message(
        content=(
            f"Connected to `{AGENT_BASE_URL}`, answering with {_model_label(status)}.\n\n"
            f"{status.get('detail', '')}\n\n"
            "Ask about a ticket, or ask me to open one — a write is proposed and waits for your "
            "approval. The settings gear changes the model and can reopen one of your earlier "
            f"conversations ({len(mapping)} available)."
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
    """Apply whatever the panel changed, and report what actually happened.

    Two independent things can arrive in one submit, because Chainlit sends the whole form: a
    different conversation, and a different model. Each is handled on its own and each is reported,
    so a refusal on one cannot silently drop the other — and the model message quotes the agent's
    reply rather than what was asked for, so "that provider has no credential in this process" is
    visible as what it is.
    """
    # The conversation first, because it decides where this turn's message will go.
    chosen = str(settings.get("conversation") or NEW_CONVERSATION)
    if chosen != cl.user_session.get("conversation"):
        await _switch_conversation(chosen)
        cl.user_session.set("conversation", chosen)

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


async def _switch_conversation(label: str) -> None:
    """Point this chat at a conversation, and redraw it.

    The redraw is why this is a function rather than one assignment. The agent keeps each
    conversation in its own store, so setting a session id alone would let the *agent* carry on from
    history the *screen* had never shown — an empty window over full memory, which is precisely the
    failure the two read endpoints were added to prevent. History cannot be re-created, only
    replayed.

    A conversation whose transcript cannot be read is still switched to, with the failure reported:
    the agent holds the history either way, and refusing the switch would leave somebody unable to
    continue a conversation they can legitimately continue.
    """
    if label == NEW_CONVERSATION:
        cl.user_session.set("session_id", _session_id())
        await cl.Message(content="Started a new conversation.").send()
        return

    mapping = cl.user_session.get("conversations")
    session_id = mapping.get(label) if isinstance(mapping, dict) else None
    if not isinstance(session_id, str) or not session_id:
        await cl.Message(
            content=(
                f"I no longer have a conversation called {label!r} — reload the page to refresh "
                "the list."
            )
        ).send()
        return

    cl.user_session.set("session_id", session_id)
    await cl.Message(content=f"Resumed **{label}**. Here is what was said:").send()

    try:
        found = await _get(f"/api/sessions/{session_id}/messages", user_id=_user_id())
    except Exception as exc:
        await cl.Message(
            content=(
                "The conversation is active, but its earlier messages could not be read, so I "
                f"cannot show them: {_error_detail(exc)}"
            )
        ).send()
        return

    for row in found.get("messages") or []:
        if isinstance(row, dict):
            await _replay_row(row)


async def _replay_row(row: dict[str, Any]) -> None:
    """One stored message, drawn as the thing it originally was.

    Three cases, decided by the role: the user's turns as user turns, the assistant's as replies,
    and a tool row as a collapsed step.

    **The third case is handling, not expectation.** The agent stores the conversation and not the
    tool calls made during it — `agent/handler.py::_turn_messages` explains why, and the short
    version is that memory is a conversation rather than a transcript — so a conversation replayed
    from this agent is user and assistant turns with no tool rows in it at all. The branch is here
    because the column exists: a row that carried a tool name would otherwise be printed as though
    the assistant had said it, which is worse than one extra branch that normally does nothing.

    A row with no text is skipped rather than drawn as an empty bubble, and that is a real case
    rather than a defensive check: an assistant turn that ended by proposing a write is stored with
    empty text, and the proposal itself belongs to the turn that produced it.
    """
    role = str(row.get("role") or "")
    content = str(row.get("content") or "")

    if role == "tool":
        async with cl.Step(name=str(row.get("tool_name") or "tool"), type="tool") as step:
            step.output = content or "(no output)"
        return

    if not content.strip():
        return

    if role == "user":
        await cl.Message(content=content, type="user_message").send()
    elif role == "assistant":
        await cl.Message(content=content).send()
    # Anything else — a system row — is the prompt the model was given, not something a person said
    # or was told, so replaying it would put words on the screen that were never in the
    # conversation.


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
