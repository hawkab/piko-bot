# Security

Pikobot can execute shell commands inside its container. Treat RabbitMQ access as privileged access to the worker runtime.

Recommended baseline:

- run Pikobot in an isolated container;
- restrict RabbitMQ credentials to the required exchange and queues;
- do not expose the gateway port to untrusted networks;
- keep `pikobot-config/config.json` outside public repositories when it contains secrets;
- use a dedicated LLM endpoint/account for this worker.
