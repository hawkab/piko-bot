"""RabbitMQ channel for AMQP-driven agent requests."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Literal

import aio_pika
from aio_pika import DeliveryMode, ExchangeType, Message
from aio_pika.abc import (
    AbstractIncomingMessage,
    AbstractQueue,
    AbstractRobustChannel,
    AbstractRobustConnection,
)
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from pikobot.bus.events import OutboundMessage
from pikobot.channels.base import BaseChannel


class RabbitMQChannelConfig(BaseModel):
    """RabbitMQ transport config kept inside channels.rabbitmq."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    enabled: bool = False
    url: str = "amqp://guest:guest@localhost:5672/"
    exchange: str = "pikobot"
    exchange_type: Literal["direct", "topic", "fanout"] = "direct"
    durable: bool = True
    prefetch_count: int = 10

    inbound_queue: str = "pikobot.inbound"
    inbound_routing_key: str = "pikobot.inbound"
    outbound_routing_key: str = "pikobot.outbound"

    sender_id_field: str = "senderId"
    chat_id_field: str = "chatId"
    content_field: str = "content"
    media_field: str = "media"
    metadata_field: str = "metadata"
    session_key_field: str = "sessionKey"
    correlation_id_field: str = "correlationId"
    reply_to_field: str = "replyTo"

    allow_from: list[str] = Field(default_factory=list)
    streaming: bool = False


class RabbitMQChannel(BaseChannel):
    """Channel implementation that consumes inbound commands from RabbitMQ."""

    name = "rabbitmq"
    display_name = "RabbitMQ"

    def __init__(self, config: Any, bus):
        parsed = RabbitMQChannelConfig.model_validate(config or {})
        super().__init__(parsed, bus)
        self.config: RabbitMQChannelConfig

        self._connection: AbstractRobustConnection | None = None
        self._channel: AbstractRobustChannel | None = None
        self._queue: AbstractQueue | None = None
        self._consumer_tag: str | None = None
        self._exchange = None
        self._stopped = asyncio.Event()

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        return RabbitMQChannelConfig().model_dump(by_alias=True)

    async def start(self) -> None:
        self._stopped.clear()
        await self._connect()
        self._running = True
        logger.info(
            "{} channel started, consuming queue='{}' via exchange='{}'",
            self.display_name,
            self.config.inbound_queue,
            self.config.exchange,
        )
        await self._stopped.wait()

    async def stop(self) -> None:
        self._running = False
        self._stopped.set()

        try:
            if self._queue and self._consumer_tag:
                await self._queue.cancel(self._consumer_tag)
        except Exception as e:
            logger.warning("{}: failed to cancel consumer: {}", self.name, e)

        try:
            if self._channel and not self._channel.is_closed:
                await self._channel.close()
        except Exception as e:
            logger.warning("{}: failed to close channel: {}", self.name, e)

        try:
            if self._connection and not self._connection.is_closed:
                await self._connection.close()
        except Exception as e:
            logger.warning("{}: failed to close connection: {}", self.name, e)

        logger.info("{} channel stopped", self.display_name)

    async def send(self, msg: OutboundMessage) -> None:
        await self._publish_outbound(msg.content, msg.metadata, msg)

    async def send_delta(
        self,
        chat_id: str,
        delta: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        msg = OutboundMessage(
            channel=self.name,
            chat_id=chat_id,
            content=delta,
            metadata=metadata or {},
        )
        await self._publish_outbound(delta, metadata or {}, msg)

    async def _connect(self) -> None:
        self._connection = await aio_pika.connect_robust(self.config.url)
        self._channel = await self._connection.channel()
        await self._channel.set_qos(prefetch_count=self.config.prefetch_count)

        self._exchange = await self._channel.declare_exchange(
            self.config.exchange,
            ExchangeType(self.config.exchange_type),
            durable=self.config.durable,
        )

        self._queue = await self._channel.declare_queue(
            self.config.inbound_queue,
            durable=self.config.durable,
        )

        if self.config.exchange_type != "fanout":
            await self._queue.bind(
                self._exchange,
                routing_key=self.config.inbound_routing_key,
            )
        else:
            await self._queue.bind(self._exchange)

        self._consumer_tag = await self._queue.consume(self._on_inbound_message)

    async def _on_inbound_message(self, message: AbstractIncomingMessage) -> None:
        try:
            payload = json.loads(message.body.decode("utf-8"))
        except Exception as e:
            logger.error("{}: invalid JSON body, dropping message: {}", self.name, e)
            await message.ack()
            return

        if not isinstance(payload, dict):
            logger.error(
                "{}: inbound payload must be an object, got {}",
                self.name,
                type(payload).__name__,
            )
            await message.ack()
            return

        sender_id = str(
            payload.get(self.config.sender_id_field)
            or message.correlation_id
            or "rabbitmq"
        )
        chat_id = str(
            payload.get(self.config.chat_id_field)
            or payload.get(self.config.reply_to_field)
            or message.reply_to
            or "default"
        )
        content_raw = payload.get(self.config.content_field, "")
        content = (
            content_raw
            if isinstance(content_raw, str)
            else json.dumps(content_raw, ensure_ascii=False)
        )

        media_raw = payload.get(self.config.media_field, [])
        media = media_raw if isinstance(media_raw, list) else []

        metadata_raw = payload.get(self.config.metadata_field, {})
        metadata = metadata_raw if isinstance(metadata_raw, dict) else {}

        correlation_id = payload.get(self.config.correlation_id_field) or message.correlation_id
        reply_to = payload.get(self.config.reply_to_field) or message.reply_to
        session_key = payload.get(self.config.session_key_field)

        if correlation_id:
            metadata.setdefault("correlationId", str(correlation_id))
        if reply_to:
            metadata.setdefault("replyTo", str(reply_to))

        metadata.setdefault(
            "amqp",
            {
                "exchange": message.exchange,
                "routingKey": message.routing_key,
                "redelivered": message.redelivered,
            },
        )

        try:
            await self._handle_message(
                sender_id=sender_id,
                chat_id=chat_id,
                content=content,
                media=media,
                metadata=metadata,
                session_key=str(session_key) if session_key else None,
            )
            await message.ack()
        except Exception:
            logger.exception("{}: failed to process inbound message", self.name)
            await message.nack(requeue=True)

    async def _publish_outbound(
        self,
        content: str,
        metadata: dict[str, Any] | None,
        msg: OutboundMessage,
    ) -> None:
        if not self._exchange or not self._channel or self._channel.is_closed:
            raise RuntimeError("RabbitMQ channel is not connected")

        meta = dict(metadata or {})
        routing_key = str(
            msg.reply_to
            or meta.get("replyTo")
            or self.config.outbound_routing_key
        )

        body = {
            "channel": self.name,
            "chatId": msg.chat_id,
            "content": content,
            "replyTo": msg.reply_to or meta.get("replyTo"),
            "correlationId": meta.get("correlationId"),
            "media": msg.media,
            "metadata": meta,
        }

        amqp_message = Message(
            body=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            content_type="application/json",
            delivery_mode=(
                DeliveryMode.PERSISTENT
                if self.config.durable
                else DeliveryMode.NOT_PERSISTENT
            ),
            correlation_id=(
                str(meta["correlationId"])
                if meta.get("correlationId") is not None
                else None
            ),
            reply_to=(
                str(msg.reply_to or meta["replyTo"])
                if (msg.reply_to or meta.get("replyTo")) is not None
                else None
            ),
        )

        publish_key = "" if self.config.exchange_type == "fanout" else routing_key
        await self._exchange.publish(amqp_message, routing_key=publish_key)
