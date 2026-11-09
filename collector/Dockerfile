# Imagem do coletor central
FROM python:3.12-slim

# ferramentas de diagnóstico de rede usadas na demonstração (ip, ss, ping, dig, tcpdump)
RUN apt-get update && apt-get install -y --no-install-recommends \
        iproute2 iputils-ping dnsutils tcpdump procps \
    && rm -rf /var/lib/apt/lists/*

# Mínimo privilégio: o processo NÃO roda como root
RUN useradd --system --uid 10001 --create-home lanwatch
WORKDIR /app
COPY collector/requirements.txt collector/
RUN pip install --no-cache-dir -r collector/requirements.txt
COPY common/ common/
COPY collector/ collector/
RUN mkdir -p /data && chown lanwatch:lanwatch /data && chmod 750 /data

USER lanwatch
ENV DATA_DIR=/data PYTHONUNBUFFERED=1
EXPOSE 5000
CMD ["python", "collector/server.py"]
