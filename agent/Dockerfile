# Imagem do agente (simula um PC da LAN house)
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        iproute2 iputils-ping dnsutils procps \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --system --uid 10002 lanwatch
WORKDIR /app
COPY agent/requirements.txt agent/
RUN pip install --no-cache-dir -r agent/requirements.txt
COPY common/ common/
COPY agent/ agent/

USER lanwatch
ENV PYTHONUNBUFFERED=1
CMD ["python", "agent/agent.py"]
