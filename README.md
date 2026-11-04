# Pikobot

RabbitMQ-driven AI agent worker for `multi-agent-executor`.

Pikobot consumes tasks from RabbitMQ, executes them with an LLM-backed agent, and publishes the result back to RabbitMQ. This fork keeps only the channel required for the Jira → RabbitMQ → Guardian → Pikobot → Jira flow.

[![Support this project · TON network](https://hawkab.github.io/support/support-button.svg)](https://hawkab.github.io/support/)

This fork contains substantial changes compared with upstream nanobot: most channels and features were removed and the RabbitMQ Jira flow was reworked. Optional contributions support development, maintenance and testing of this fork. The [support page](https://hawkab.github.io/support/) has a QR code, wallet link and copy buttons. You choose the amount in your wallet.

## What was kept

- RabbitMQ channel.
- CLI and gateway runtime.
- LLM providers and local Ollama-compatible provider support.
- Agent tools needed to work inside the container.

## What was removed

- WhatsApp bridge.
- Telegram, Slack, Matrix, Discord, Email, DingTalk, Feishu, Mochat, QQ, WeCom and Weixin channels.
- Node.js bridge build.
- Upstream demo media and channel plugin documentation.

## Requirements

- Docker.
- Docker Compose v2.
- RabbitMQ reachable from the container.
- Ollama or another OpenAI-compatible LLM endpoint reachable from the container.

## Configuration

Default config path:

```text
./pikobot-config/config.json
```

Default RabbitMQ names:

```text
exchange: pikobot
inbound queue: pikobot.inbound
inbound routing key: pikobot.inbound
outbound routing key: pikobot.outbound
```

If other services still publish to `nanobot.*`, update them to `pikobot.*` or change `pikobot-config/config.json` back to the old queue names.

## Run

```bash
docker compose up -d --build pikobot-gateway
```

## Redeploy

```bash
docker compose down
docker compose up -d --build pikobot-gateway
```

## Stop

```bash
docker compose down
```

## Logs

```bash
docker compose logs -f pikobot-gateway
```

## CLI status

```bash
docker compose run --rm pikobot-cli status
```

## Agent Factory embedding

The optional [`pikobot.factory`](docs/agent-factory.md) profile embeds the same
`AgentRunner` behind a scoped MCP broker. It adds durable checkpoints, recovery
without replaying completed effects, scoped tool catalogs and task-bound user
questions. The existing RabbitMQ gateway remains available.

### Run the portable regression tests

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install loguru
python -m unittest discover -s tests -v
```
