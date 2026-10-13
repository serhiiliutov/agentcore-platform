"""The front-end page, as one self-contained document.

Held as a module constant rather than a file on disk so the service has no static-file mount and
no path that has to exist at runtime — one less thing for a container to get wrong, and the page
cannot go missing from a built image.

**No framework, and no build step.** The assignment says the page is not scored on visual design,
so the honest thing is to spend nothing on tooling: a browser can open this as-is, and there is no
`node_modules` between a reviewer and a working demo. The CSS and JS below are the whole of it.

**It talks to the contract that already exists.** Sending a message is `POST /invocations`;
approving is `POST /invocations/approvals/{id}/decide`. Neither was written for the page, which is
the point — the UI is a client of the documented surface, not a second implementation of it. If the
page needed an endpoint of its own, the contract would be incomplete.

**The pending list is fetched on load, not only after a turn.** An approval survives a restart of
the service, so a proposal can be outstanding before the page is opened or after it was last used.
A page that only showed proposals it had just created would make the durability the backend went to
the trouble of providing invisible from the front end — and a pending write nobody can see is a
pending write nobody will approve.
"""

from __future__ import annotations

#: The whole UI. Kept as one string so there is nothing to build, bundle or mount.
INDEX_HEAD = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AgentCore - IT Helpdesk Agent</title>
<style>
  :root {
    --bg: #12141a;
    --panel: #1b1e27;
    --line: #2b3040;
    --ink: #e7eaf0;
    --muted: #8b93a7;
    --accent: #6aa2ff;
    --ok: #4bd08a;
    --bad: #ff7a7a;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    background: var(--bg);
    color: var(--ink);
    font: 15px/1.55 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
  }
  header {
    padding: 14px 20px;
    border-bottom: 1px solid var(--line);
    display: flex;
    gap: 16px;
    align-items: center;
    flex-wrap: wrap;
  }
  header h1 { font-size: 15px; margin: 0; font-weight: 600; letter-spacing: .01em; }
  header .who { margin-left: auto; display: flex; gap: 8px; align-items: center; }
  label { color: var(--muted); font-size: 13px; }
  main { padding: 20px; max-width: 860px; margin: 0 auto; }
  input, button {
    font: inherit;
    color: var(--ink);
    background: var(--panel);
    border: 1px solid var(--line);
    border-radius: 8px;
    padding: 8px 10px;
  }
  input:focus { outline: 2px solid var(--accent); outline-offset: 1px; }
  button { cursor: pointer; }
  button:hover { border-color: var(--accent); }
  button.primary { background: var(--accent); color: #0d1017; border-color: var(--accent); font-weight: 600; }
  button.ok { border-color: var(--ok); color: var(--ok); }
  button.bad { border-color: var(--bad); color: var(--bad); }
  button:disabled { opacity: .5; cursor: default; }
  form { display: flex; gap: 8px; margin-top: 4px; }
  form input { flex: 1; }
  .turn { border: 1px solid var(--line); border-radius: 10px; padding: 14px; margin: 14px 0; background: var(--panel); }
  .turn.user { border-color: #33405c; }
  .role { font-size: 12px; color: var(--muted); text-transform: uppercase; letter-spacing: .06em; margin-bottom: 6px; }
  .said { white-space: pre-wrap; }
  .calls { margin-top: 12px; border-top: 1px dashed var(--line); padding-top: 10px; }
  .call { display: flex; gap: 8px; align-items: baseline; padding: 3px 0; font-size: 14px; flex-wrap: wrap; }
  .call .name { font-family: ui-monospace, "Cascadia Code", Menlo, monospace; color: var(--accent); }
  .call .args { color: var(--muted); font-family: ui-monospace, "Cascadia Code", Menlo, monospace; font-size: 12px; }
  .call.err .name { color: var(--bad); }
  .meta { font-size: 12px; color: var(--muted); margin-top: 10px; }
  .approval { border: 1px solid var(--accent); border-radius: 10px; padding: 14px; margin: 14px 0; background: #17202f; }
  .approval h2 { font-size: 14px; margin: 0 0 6px; }
  .approval .payload { font-family: ui-monospace, "Cascadia Code", Menlo, monospace; font-size: 12px; color: var(--muted); white-space: pre-wrap; margin: 8px 0; }
  .approval .row { display: flex; gap: 8px; margin-top: 10px; }
  .banner { border-radius: 8px; padding: 10px 12px; margin: 14px 0; border: 1px solid var(--bad); color: var(--bad); font-size: 14px; }
  .hint { color: var(--muted); font-size: 13px; }
  [hidden] { display: none !important; }
</style>
</head>
<body>
<header>
  <h1>AgentCore &middot; IT Helpdesk Agent</h1>
  <div class="who">
    <label for="user">user</label>
    <input id="user" value="alice" size="10" autocomplete="off">
    <label for="session">session</label>
    <input id="session" value="" size="14" placeholder="new" autocomplete="off">
  </div>
</header>

<main>
  <div id="banner" class="banner" hidden></div>

  <section id="pending"></section>

  <section id="transcript"></section>

  <form id="ask" autocomplete="off">
    <input id="message" placeholder="Describe the problem, e.g. the printer is jammed again" required>
    <button class="primary" type="submit">Send</button>
  </form>
  <p class="hint">Reads run immediately. A write is proposed and waits for your approval above.</p>
</main>
"""
#: The script, kept separate from the markup only so the two are readable in one file each.
INDEX_SCRIPT = """
<script>
(function () {
  "use strict";

  var transcript = document.getElementById("transcript");
  var pending = document.getElementById("pending");
  var banner = document.getElementById("banner");
  var form = document.getElementById("ask");
  var message = document.getElementById("message");
  var userField = document.getElementById("user");
  var sessionField = document.getElementById("session");
  var sendButton = form.querySelector("button");

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function showError(text) {
    banner.textContent = text;
    banner.hidden = false;
  }

  function clearError() { banner.hidden = true; }

  function user() { return userField.value.trim() || "alice"; }
  function session() { return sessionField.value.trim(); }

  async function json(url, options) {
    var response = await fetch(url, options);
    var text = await response.text();
    var body = null;
    if (text) {
      try { body = JSON.parse(text); } catch (e) { body = null; }
    }
    if (!response.ok) {
      var detail = body && body.error ? body.error.detail : (text || response.statusText);
      throw new Error(detail || ("HTTP " + response.status));
    }
    return body;
  }

  function renderTurn(role, said, result) {
    var turn = el("div", "turn " + role);
    turn.appendChild(el("div", "role", role === "user" ? "you" : "agent"));
    turn.appendChild(el("div", "said", said));

    if (result && result.tool_calls && result.tool_calls.length) {
      var calls = el("div", "calls");
      result.tool_calls.forEach(function (call) {
        var row = el("div", call.is_error ? "call err" : "call");
        row.appendChild(el("span", "name", call.name));
        var args = JSON.stringify(call.arguments || {});
        if (args && args !== "{}") row.appendChild(el("span", "args", args));
        if (call.summary) row.appendChild(el("span", "args", call.summary));
        calls.appendChild(row);
      });
      turn.appendChild(calls);
    }

    if (result) {
      var bits = [];
      if (result.stop_reason) bits.push(result.stop_reason);
      if (typeof result.steps === "number") bits.push(result.steps + " step(s)");
      if (bits.length) turn.appendChild(el("div", "meta", bits.join(" - ")));
    }

    transcript.insertBefore(turn, transcript.firstChild);
  }

  function renderApproval(proposal) {
    var card = el("div", "approval");
    card.setAttribute("data-id", proposal.approval_id);
    card.appendChild(el("h2", null, "Approval needed: " + proposal.tool_name));
    if (proposal.summary) card.appendChild(el("div", "hint", proposal.summary));
    card.appendChild(el("div", "payload", JSON.stringify(proposal.arguments || {}, null, 2)));
    if (proposal.expires_at) {
      card.appendChild(el("div", "meta", "Expires " + proposal.expires_at));
    }

    var row = el("div", "row");
    var approve = el("button", "ok", "Approve and run");
    var decline = el("button", "bad", "Decline");
    row.appendChild(approve);
    row.appendChild(decline);
    card.appendChild(row);

    function submit(approved) {
      approve.disabled = true;
      decline.disabled = true;
      clearError();
      json("invocations/approvals/" + encodeURIComponent(proposal.approval_id) + "/decide", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ user_id: user(), approved: approved })
      }).then(function (result) {
        card.remove();
        var said = result.detail || (approved ? "Done." : "Declined.");
        var turn = el("div", "turn");
        turn.appendChild(el("div", "role", approved ? "write " + result.outcome : "declined"));
        turn.appendChild(el("div", "said", said));
        if (result.replayed) {
          turn.appendChild(el("div", "meta", "The backend reported this was already done."));
        }
        transcript.insertBefore(turn, transcript.firstChild);
      }).catch(function (error) {
        approve.disabled = false;
        decline.disabled = false;
        showError(error.message);
      });
    }

    approve.addEventListener("click", function () { submit(true); });
    decline.addEventListener("click", function () { submit(false); });
    return card;
  }

  async function loadPending() {
    pending.textContent = "";
    try {
      var found = await json("api/approvals?user_id=" + encodeURIComponent(user()));
      (found.approvals || []).forEach(function (proposal) {
        pending.appendChild(renderApproval(proposal));
      });
    } catch (error) {
      // A missing listing must not stop the page working: sending a message is still possible,
      // and the failure is shown rather than swallowed.
      showError("Could not load pending approvals: " + error.message);
    }
  }

  form.addEventListener("submit", async function (event) {
    event.preventDefault();
    var text = message.value.trim();
    if (!text) return;
    clearError();
    sendButton.disabled = true;
    renderTurn("user", text, null);

    try {
      var body = { prompt: text, user_id: user() };
      if (session()) body.session_id = session();
      var result = await json("invocations", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body)
      });
      if (result.session_id) sessionField.value = result.session_id;
      renderTurn("agent", result.output || "(the agent proposed a write - see below)", result);
      if (result.pending_approval) {
        pending.appendChild(renderApproval(result.pending_approval));
      }
      message.value = "";
    } catch (error) {
      showError(error.message);
    } finally {
      sendButton.disabled = false;
      message.focus();
    }
  });

  userField.addEventListener("change", loadPending);
  loadPending();
  message.focus();
})();
</script>
</body>
</html>
"""

#: The complete document, in the order a browser reads it.
INDEX_HTML = INDEX_HEAD + INDEX_SCRIPT
