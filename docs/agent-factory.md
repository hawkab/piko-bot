# Agent Factory embedding

This profile is the adaptation used by the Agent Factory installer. It is based
on pikobot commit `4d8677802f807d5fb1aaf8dfb3d9d3f938c23036` and imports the
existing `pikobot.agent.runner.AgentRunner`.

## Execution and recovery

- The journal persists request intents, actual tool results, checkpoints and
  final results with ownership checks, an exclusive lock and fsync.
- Restarting a session retains completed tool results. An interrupted operation
  with an unknown outcome requires reconciliation with the owning service.
- Tool definitions come from the scoped MCP catalog. Catalog changes are read
  after their triggering result has been durably recorded.
- Accepted suites are frozen and their mutation tools are removed from the
  active catalog. Completion follows the broker's explicit terminal receipt.
- Mandatory task context is read before inference. Context recovery retains
  completed work; an ordinary provider error does not permit blind replay.
- Task-bound user questions suspend the role and resume from a durable
  checkpoint after the matching response.
- Durable inference request IDs support reattachment and preserve actual token
  usage and elapsed-time accounting.

## Core runner additions

`AgentRunSpec.max_iterations` accepts `None` for runs without an iteration cap.
An explicit positive integer retains bounded execution. Token usage accumulates
across model turns; hooks receive the usage of their current turn. The new
`on_context_rejected` and `should_stop` hooks support checkpoint recovery and
termination after a service-confirmed tool result without another generation.
Core exports and tokenizer imports are lazy so a headless runner does not load
personal tools or channels.

## Deployment

The Agent Factory installer supplies the companion `agent_factory` package,
scoped MCP broker, inference gateway, role instructions and generated runtime
configuration. `pikobot.factory` uses that companion package for schema
validation, prompt binding and recovery; install it through the Factory installer.
The existing Docker/RabbitMQ gateway does not require the Factory profile.

The installer invokes the embedding with a generated configuration:

```text
python -m pikobot.factory --config /path/to/generated/config.json TASK_PROMPT
```

Keep service credentials in the configured environment or protected credential
files. Session journals contain task context and tool results and belong in a
private runtime directory, outside Git.

Portable tests in this repository cover journal integrity and the core runner.
The companion Agent Factory suite covers broker, provider, catalog, user-wait and
recovery integration with deterministic fixtures.
