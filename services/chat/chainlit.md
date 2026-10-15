# AgentCore Helpdesk

An IT helpdesk agent, and the approval gate in front of every write it proposes.

Ask about a ticket — find one, read one — and the agent answers from the ticketing system, showing each tool call it made underneath the reply. Ask it to **open a ticket or comment on one**, and it stops: the proposal appears as a card with the exact arguments it will run with, and two buttons. Nothing reaches the ticketing system until you press **Approve**.

That gate is the point of the whole exercise, and it is not a convention in this front end: the pending approval is a row in the agent's database, so it survives a restart of either process, and the same decision can equally be given by answering the agent's next message with "yes, go ahead" or by calling the API directly. This UI is one client of that contract, not the contract.

## What to try

| Say this | What happens |
|---|---|
| `find tickets about the printer` | the agent searches and shows you the tool call it used |
| `what is the status of TCK-1002?` | it reads one ticket, with its comments |
| `open a ticket for the flickering monitor` | it **proposes** a write and waits — nothing is created yet |
| `no thanks` (after a proposal) | the write is declined and nothing is written |

Answering `yes, go ahead` in the chat is the approval, exactly as the buttons are. Asking something unrelated leaves the proposal pending rather than silently deciding it.

## Where the answers come from

The agent decides which tool to call; the ticketing system enforces what the values may be. The settings gear in the header chooses the model — only providers this deployment can actually build are offered, and the choice is stored by the agent rather than here, so it outlives a restart of this process.

If nothing is answering, this front end says so with the URL it tried. The agent is a separate service; start it first.
