Assignment: Build an agentic system

Build a small agent that gets work done through tools and runs on your own
machine. Then tell us how you would run it on AWS with Amazon Bedrock
AgentCore.
You don&#39;t need an AWS account for this assignment.
Pick one of the options below. The requirements are the same for all three, so
choose the one that interests you most.
Option 1: IT Helpdesk Agent
An employee describes a problem. The agent searches existing tickets, checks the
status of the ones that match, and raises a new ticket or adds a comment to an
existing one. You&#39;ll build a mock ticketing system for it to work with.
Option 2: Travel Booking Agent
An employee asks for a work trip. The agent searches a catalogue of flights and
hotels, checks the options against a travel policy (a handful of rules is enough), and
books the trip. You&#39;ll build a mock booking system and write the policy.
Option 3: Lab Supplies Agent
A scientist asks for reagents or consumables. The agent checks what is in stock,
finds catalogue items from approved suppliers, and raises a purchase order. You&#39;ll
build a mock inventory and ordering system.
In each option the agent looks things up and then writes one change to your mock: a
new ticket, a booking or a purchase order.
What to Build
Use any agent framework (LangGraph, Strands Agents, the OpenAI Agents SDK, or
plain code) and any model provider, including a local model. Write it in Python or
TypeScript, whichever you&#39;re stronger in.
1. An agent service. An HTTP endpoint that takes a user id, a session id and a
message and returns the agent&#39;s reply. The model provider should be
swappable through config.
2. Tools behind an MCP server you write. Wrap your mock in an MCP server
with at least a search tool (paginated), a read tool and the write tool. The
agent reaches your mock only through this server, and the server

authenticates to the mock with a key or token. Tool names, schemas,
descriptions and error messages are up to you.
3. Approval before the write. The agent proposes the write and stops. The
user approves or declines in their next message, and only then does the write
run. A pending approval must survive a restart of your service.
4. Memory. Within a session the agent keeps track of the conversation. Across
sessions it remembers at least one useful fact about the user, such as their
team, home airport or lab. One user never sees another user&#39;s memory.
5. Failure handling. Give your mock a switch that makes it fail some of the time:
rate limits, server errors and slow responses. With the switch on, the agent
should retry sensibly, give up after a timeout, tell the user when it can&#39;t finish,
and never make the same write twice.
6. Evals. A set of cases you can run with one command. Cover at least: the right
tool chosen, the approval step respected, an out-of-scope request refused,
and a run with failures switched on.
7. Traces. For each request, record what happened: model calls, tool calls with
arguments and results, latency, tokens and errors. Structured logs,
OpenTelemetry or a local Langfuse are all fine.
Build a thin web page as the front end. It should show the tool calls and let the user
approve or decline the write.
Optional, pick at most one: a second agent with a handoff (a router passing to a
specialist, or a policy checker the main agent consults), streaming responses, or
cancelling a request that is still running. Only add one if it fits your design.
What to Submit
1. GitHub repo with your code
2. README.md / other docs in the repo with:
a. Quick setup instructions
b. Architecture overview (a simple diagram is great but not required)
c. Agent design: frameworks and models you considered and what you
chose, how state and the approval step work, and what your memory
keeps, where and for how long
d. Tool design: how you named and described your tools, how errors
reach the model, and how the MCP server authenticates to your mock
e. Quality: your eval cases, your latest results, what the evals don&#39;t catch
yet, and any guardrails you added
f. Observability: what you trace and how you&#39;d use it to debug a bad run
g. Key technical decisions you made and why
h. Engineering standards you&#39;ve followed (and maybe some that you
skipped)

i. How you used AI tools in your development process
j. What you&#39;d do differently with more time
k. Note: We need your thoughts, not an LLM&#39;s direct output
3. AGENTCORE.md: how you would run this on AWS with AgentCore (details
below)
4. Screenshots or a short recording of one run from start to finish: a read, a
proposed write, the approval, and a failure being handled. Include the output
of your eval run.
The AgentCore Design Doc
In AGENTCORE.md, tell us how you would run what you built on AWS with Amazon
Bedrock AgentCore. You don&#39;t deploy anything.
Base it on your build: which parts of AgentCore you&#39;d use and why, what you&#39;d keep
or change from your local version, and where you expect it to get difficult. Include a
diagram.
If you haven&#39;t used AgentCore, work from the AWS documentation and tell us what
you&#39;d want to check before relying on it. It doesn&#39;t need to be long.
What We&#39;re Looking For
• Agent design: state and orchestration that still behave correctly when the
service restarts or the backend fails.
• Tool design: tools a model picks correctly, with schemas, descriptions and
error messages it can act on.
• Memory: clear choices about what is remembered, where, for how long and
for whom.
• Quality you can measure: evals that would catch a regression, and what
your results tell you.
• Observability: you can explain any run from its trace.
• Platform thinking: an AgentCore design that fits your build and is accurate
about the platform.
• Engineering excellence: clean, readable, well-structured code that others
can understand and build on. Ideally a simple containerised, well tested and
observable solution.
• AI-assisted development approach: You&#39;re encouraged to use AI coding
tools of your choice. We&#39;re interested in how you use them and ensure that
the code is according to your preference. How do you make it repeatable and
maintainable? What are your do&#39;s and don&#39;ts with AI coding assistants?

We don&#39;t score visual design, so keep the web page simple and spend your time on
the agent.
Tips &amp; Notes
• Start simple, then enhance: A basic working version with great engineering
&gt; a complex broken version. We won&#39;t judge you on handling every edge
case, but do acknowledge them in your README.
• Keep the mock small: a few seeded records in SQLite or a JSON file are
enough. Its job is to give your agent something to call and to fail when you tell
it to.
• We want your thoughts and ideas (not an LLM&#39;s outputs): You&#39;re free to
use AI coding assistants, but the README and AGENTCORE.md should
reflect your own thinking. Use your judgement on where LLM output is fine
and where you need to say it yourself.
• Not enough time to implement everything? That&#39;s okay! Document what
you&#39;d add next in your README.
Remember, we value a solid &amp; well-engineered basic solution A LOT MORE
than an over-engineered complex one.