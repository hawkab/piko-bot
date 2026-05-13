"""RabbitMQ-only channel registry for Pikobot."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pikobot.channels.base import BaseChannel


def discover_channel_names() -> list[str]:
    """Return built-in channel names supported by this fork."""
    return ["rabbitmq"]


def load_channel_class(module_name: str) -> type[BaseChannel]:
    """Return the channel class for a supported channel name."""
    if module_name != "rabbitmq":
        raise ImportError(f"Unsupported channel: {module_name}")

    from pikobot.channels.rabbitmq import RabbitMQChannel

    return RabbitMQChannel


def discover_plugins() -> dict[str, type[BaseChannel]]:
    """External channel plugins are disabled in this fork."""
    return {}


def discover_all() -> dict[str, type[BaseChannel]]:
    """Return all channels available in this fork."""
    from pikobot.channels.rabbitmq import RabbitMQChannel

    return {"rabbitmq": RabbitMQChannel}
