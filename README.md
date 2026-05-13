# Pikobot

RabbitMQ-driven AI agent worker for `multi-agent-executor`.

Pikobot consumes tasks from RabbitMQ, executes them with an LLM-backed agent, and publishes the result back to RabbitMQ. This fork keeps only the channel required for the Jira → RabbitMQ → Guardian → Pikobot → Jira flow.

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
