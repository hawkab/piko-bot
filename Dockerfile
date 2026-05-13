FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      ca-certificates \
      curl \
      git \
      openssh-client \
      procps \
      iproute2 && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
RUN mkdir -p pikobot && touch pikobot/__init__.py && \
    uv pip install --system --no-cache . && \
    rm -rf pikobot

COPY pikobot/ pikobot/
COPY bootstrap_env.py entrypoint.sh ./
RUN chmod +x /app/entrypoint.sh && \
    uv pip install --system --no-cache . && \
    mkdir -p /root/.pikobot

EXPOSE 18790

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["status"]
