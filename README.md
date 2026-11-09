# LANWatch

Monitoramento em tempo real dos PCs de uma LAN house / arena de e-sports.
Projeto final de **PSI129A – Conectividade em Sistemas Ciberfísicos** (PUCPR, 2026/2).

Cada PC roda um **agente Python** (psutil) que envia CPU, memória, disco, rede e latência por **TCP** a um **coletor central multithread**, que guarda o histórico em SQLite e detecta máquinas offline ou travadas.

> Status: **Checkpoint 1**, com ambiente montado e comunicação cliente/servidor funcionando.
> Detalhes de arquitetura, plano de rede e testes em [`docs/CHECKPOINT1.md`](docs/CHECKPOINT1.md).

## Estrutura

```
lanwatch/
├── common/protocol.py      # protocolo JSON Lines (framing, validação de tamanho)
├── collector/server.py     # coletor TCP: thread por agente, SQLite, logs
├── collector/Dockerfile
├── agent/agent.py          # agente psutil: ping/pong, métricas, reconexão com backoff
├── agent/Dockerfile
├── docker-compose.yml      # rede 172.28.10.0/24, 1 coletor + 3 agentes
├── .env.example            # token compartilhado
└── docs/                   # checkpoint, evidências dos testes
```

## Requisitos

- Docker Engine + Docker Compose v2 (Docker Desktop no Windows já inclui os dois, via WSL 2)
- Para rodar um agente fora do Docker: Python 3.10+ e `pip install psutil`

## Como executar

```bash
cp .env.example .env
# edite .env e coloque um token forte:
python3 -c "import secrets; print(secrets.token_hex(16))"

docker compose up -d --build
docker compose logs -f collector        # ver as métricas chegando
```

Parar tudo: `docker compose down` (com `-v` apaga também o histórico).

### Rodando um agente num PC real (WSL 2 / Linux / Windows)

```bash
pip install psutil
COLLECTOR_HOST=<IP do host do Docker> AGENT_TOKEN=<mesmo token> AGENT_NAME=pc-rafael \
  python3 agent/agent.py
```

## Variáveis de ambiente

| Variável | Onde | Padrão | Descrição |
|---|---|---|---|
| `AGENT_TOKEN` | ambos | — (obrigatória) | segredo compartilhado do handshake |
| `COLLECTOR_PORT` | ambos | `5000` | porta TCP de ingestão |
| `COLLECTOR_HOST` | agente | `127.0.0.1` | nome/IP do coletor |
| `AGENT_NAME` | agente | hostname | identificador do PC (`[A-Za-z0-9_-]`, até 32 caracteres) |
| `INTERVAL` | agente | `2` | segundos entre envios |
| `CLIENT_TIMEOUT` | coletor | `15` | segundos sem mensagem até marcar OFFLINE |
| `MAX_CLIENTS` | coletor | `50` | limite de agentes simultâneos |
| `DATA_DIR` | coletor | `./data` | banco SQLite e logs |

## Comandos de diagnóstico usados

```bash
docker exec collector ip -4 addr show eth0
docker exec collector ss -tnp state established '( sport = :5000 )'
docker exec pc01 ping -c 3 collector
docker exec pc01 getent hosts collector
docker exec collector ps -o user,pid,nlwp,cmd -C python     # NLWP = nº de threads
docker exec -u root collector tcpdump -i eth0 -nn -A 'tcp port 5000'
docker exec collector tail -f /data/logs/collector.log
docker pause pc03      # simula PC travado → OFFLINE após 15 s
docker stop collector  # simula servidor fora do ar → agentes reconectam
```

## Segurança aplicada

- Autenticação por token no handshake (`hmac.compare_digest`)
- Processos sem root (`USER lanwatch` nos Dockerfiles); `/data` com permissão `750`
- Validação de tipo e faixa de cada campo; limite de 64 KB por mensagem; limite de 50 conexões
- *(CP2)* TLS no canal e firewall `ufw`
