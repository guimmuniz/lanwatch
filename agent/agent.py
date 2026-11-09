"""
agent.py — Agente do LANWatch, roda em cada PC da LAN house.

A cada INTERVAL segundos:
  1. mede a latência até o coletor (ping/pong na própria conexão TCP);
  2. lê CPU, memória, disco, rede e nº de processos do SO com psutil;
  3. envia tudo ao coletor como uma linha JSON.

Robustez:
  - Se o coletor estiver fora do ar (conexão recusada, timeout, conexão
    resetada), o agente NÃO morre: espera e tenta de novo com backoff
    exponencial (1 s, 2 s, 4 s ... até 30 s), com um pouco de aleatoriedade
    ("jitter") para que 30 PCs não reconectem todos no mesmo instante.
"""

import logging
import os
import signal
import random
import socket
import sys
import time

import psutil

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "common"))
from protocol import PROTOCOL_VERSION, LineReader, ProtocolError, encode  # noqa: E402

COLLECTOR_HOST = os.getenv("COLLECTOR_HOST", "127.0.0.1")
COLLECTOR_PORT = int(os.getenv("COLLECTOR_PORT", "5000"))
TOKEN = os.getenv("AGENT_TOKEN", "")
NAME = os.getenv("AGENT_NAME", socket.gethostname())
INTERVAL = float(os.getenv("INTERVAL", "2"))
IO_TIMEOUT = 5.0     # tempo máximo esperando o coletor responder
BACKOFF_MAX = 30.0
_authenticated = False  # vira True quando a sessão atual passou do handshake

logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                    format=f"%(asctime)s [%(levelname)s] [{NAME}] %(message)s")
log = logging.getLogger("agent")


class AuthError(Exception):
    """O coletor recusou o token — não adianta tentar de novo em loop rápido."""


def collect() -> dict:
    """Lê os recursos do sistema operacional (via /proc no Linux)."""
    net = psutil.net_io_counters()
    return {
        "type": "metrics",
        "host": NAME,
        "ts": time.time(),
        "cpu": psutil.cpu_percent(interval=None),   # % desde a chamada anterior
        "mem": psutil.virtual_memory().percent,
        "disk": psutil.disk_usage("/").percent,
        "net_tx": net.bytes_sent,
        "net_rx": net.bytes_recv,
        "procs": len(psutil.pids()),
    }


def session():
    """Uma sessão = uma conexão TCP, do connect() até cair."""
    log.info("conectando a %s:%d ...", COLLECTOR_HOST, COLLECTOR_PORT)
    sock = socket.create_connection((COLLECTOR_HOST, COLLECTOR_PORT), timeout=IO_TIMEOUT)
    try:
        reader = LineReader(sock)
        sock.sendall(encode({"type": "hello", "host": NAME, "token": TOKEN,
                             "version": PROTOCOL_VERSION}))
        reply = reader.read_message()
        if reply is None:
            raise ConnectionError("coletor fechou a conexão no handshake")
        if reply.get("type") == "error":
            raise AuthError(reply.get("reason"))
        if reply.get("type") != "welcome":
            raise ProtocolError(f"resposta inesperada: {reply}")
        log.info("conectado e autenticado (protocolo v%s)", reply.get("version"))
        global _authenticated
        _authenticated = True

        psutil.cpu_percent(interval=None)  # 1ª leitura sempre dá 0; "aquece" o contador
        while True:
            # --- latência de aplicação (RTT) medida na própria conexão
            t0 = time.perf_counter()
            sock.sendall(encode({"type": "ping", "t": t0}))
            pong = reader.read_message()
            if pong is None:
                raise ConnectionError("coletor fechou a conexão")
            rtt_ms = (time.perf_counter() - t0) * 1000

            data = collect()
            data["rtt_ms"] = round(rtt_ms, 3)
            sock.sendall(encode(data))
            log.info("enviado cpu=%.1f%% mem=%.1f%% rtt=%.2fms", data["cpu"], data["mem"], rtt_ms)
            time.sleep(INTERVAL)
    finally:
        sock.close()


def _on_sigterm(signum, frame):
    """docker stop / systemctl stop mandam SIGTERM: encerramos de forma limpa."""
    raise KeyboardInterrupt


def main():
    signal.signal(signal.SIGTERM, _on_sigterm)
    if not TOKEN:
        sys.exit("ERRO: defina AGENT_TOKEN")
    global _authenticated
    delay = 1.0
    while True:
        _authenticated = False
        try:
            session()
        except AuthError as exc:
            log.error("autenticação recusada: %s — nova tentativa em %ds", exc, BACKOFF_MAX)
            time.sleep(BACKOFF_MAX)
            continue
        except ConnectionRefusedError:
            log.warning("conexão recusada (coletor fora do ar?)")
        except socket.timeout:
            log.warning("timeout falando com o coletor")
        except socket.gaierror as exc:
            log.warning("falha de DNS ao resolver '%s': %s", COLLECTOR_HOST, exc)
        except (ConnectionError, OSError, ProtocolError) as exc:
            log.warning("conexão perdida: %s", exc)
        except KeyboardInterrupt:
            log.info("encerrando agente")
            return
        if _authenticated:  # a sessão chegou a funcionar: recomeça o backoff do zero
            delay = 1.0
        wait = min(delay, BACKOFF_MAX) + random.uniform(0, 0.5)
        log.info("reconectando em %.1fs", wait)
        time.sleep(wait)
        delay = min(delay * 2, BACKOFF_MAX)


if __name__ == "__main__":
    main()
