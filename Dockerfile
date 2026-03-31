FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

RUN apt-get update && apt-get install -y \
    git curl ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Clone hermes-agent (shallow for speed)
WORKDIR /opt/hermes
RUN git clone --depth 1 https://github.com/NousResearch/hermes-agent.git .

# Install hermes + all optional deps via uv
RUN uv pip install --system -e ".[all]"

# Install our wrapper server deps
COPY requirements.txt /app/requirements.txt
RUN uv pip install --system -r /app/requirements.txt

COPY server.py /app/server.py
COPY start.sh /app/start.sh
RUN chmod +x /app/start.sh

ENV HOME=/data
ENV HERMES_HOME=/data/.hermes

ENTRYPOINT ["/app/start.sh"]
