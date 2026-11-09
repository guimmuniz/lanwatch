"""
server.py — Coletor central do LANWatch.

Recebe métricas dos agentes (um por PC da LAN house) via TCP, porta 5000.

Concorrência: um thread por agente conectado (modelo "thread-per-connection").
  - A thread principal só faz accept() e entrega cada conexão a uma nova thread.
  - Assim um agente lento ou travado não bloqueia os outros.
  - Estado compartilhado (dict de hosts e o banco SQLite) é protegido por Lock,
    porque várias threads escrevem nele ao mesmo tempo (condição de corrida).

Por que TCP e não UDP?
  - Métricas precisam chegar completas e em ordem para os gráficos/alertas.
  - A conexão persistente é usada como "sinal de vida": se ela cai (FIN, RST ou
    timeout), sabemos na hora que o PC caiu — com UDP teríamos que inventar isso.
"""

import hmac
import logging
import logging.handlers
import os
import signal
import socket
import sqlite3
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "common"))
from protocol import PROTOCOL_VERSION, LineReader, ProtocolError, encode  # noqa: E402

# ---------------------------------------------------------------- configuração
HOST = os.getenv("COLLECTOR_BIND", "0.0.0.0")
PORT = int(os.getenv("COLLECTOR_PORT", "5000"))
TOKEN = os.getenv("AGENT_TOKEN", "")
DATA_DIR = os.getenv("DATA_DIR", "./data")
CLIENT_TIMEOUT = float(os.getenv("CLIENT_TIMEOUT", "15"))  # s sem mensagem = agente morto
MAX_CLIENTS = int(os.getenv("MAX_CLIENTS", "50"))

if not TOKEN:
    sys.exit("ERRO: defina AGENT_TOKEN (veja .env.example)")

# Diretório de dados com permissão 750: dono lê/escreve, grupo lê, outros nada.
os.makedirs(os.path.join(DATA_DIR, "logs"), exist_ok=True)
os.chmod(DATA_DIR, 0o750)

# ---------------------------------------------------------------- logs
log = logging.getLogger("collector")
log.setLevel(logging.INFO)
_fmt = logging.Formatter("%(asctime)s [%(levelname)s] [%(threadName)s] %(message)s")
_console = logging.StreamHandler(sys.stdout)
_console.setFormatter(_fmt)
_file = logging.handlers.RotatingFileHandler(  # arquivo gira ao chegar em 1 MB, guarda 3
    os.path.join(DATA_DIR, "logs", "collector.log"), maxBytes=1_000_000, backupCount=3
)
_file.setFormatter(_fmt)
log.addHandler(_console)
log.addHandler(_file)

# ---------------------------------------------------------------- estado compartilhado
db_lock = threading.Lock()
db = sqlite3.connect(os.path.join(DATA_DIR, "lanwatch.db"), check_same_thread=False)
db.execute(
    """CREATE TABLE IF NOT EXISTS metrics (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        host TEXT NOT NULL, ts REAL NOT NULL,
        cpu REAL, mem REAL, disk REAL,
        net_tx INTEGER, net_rx INTEGER, procs INTEGER, rtt_ms REAL
    )"""
)
db.execute("CREATE INDEX IF NOT EXISTS idx_metrics_host_ts ON metrics(host, ts)")
db.commit()

hosts_lock = threading.Lock()
hosts: dict[str, dict] = {}  # host -> {"addr", "online", "last_seen", "last"}

client_slots = threading.BoundedSemaphore(MAX_CLIENTS)  # limita threads simultâneas


# ---------------------------------------------------------------- validação
def _num(msg, key, lo, hi):
    """Lê um campo numérico e garante que está no intervalo [lo, hi]."""
    v = msg.get(key)
    if not isinstance(v, (int, float)) or isinstance(v, bool) or not lo <= v <= hi:
        raise ProtocolError(f"campo '{key}' inválido: {v!r}")
    return v


def validate_metrics(msg: dict) -> dict:
    return {
        "ts": _num(msg, "ts", 0, 1e11),
        "cpu": _num(msg, "cpu", 0, 100),
        "mem": _num(msg, "mem", 0, 100),
        "disk": _num(msg, "disk", 0, 100),
        "net_tx": int(_num(msg, "net_tx", 0, 2**63)),
        "net_rx": int(_num(msg, "net_rx", 0, 2**63)),
        "procs": int(_num(msg, "procs", 0, 1_000_000)),
        "rtt_ms": _num(msg, "rtt_ms", 0, 60_000) if msg.get("rtt_ms") is not None else None,
    }


def valid_hostname(name) -> bool:
    return isinstance(name, str) and 1 <= len(name) <= 32 and all(
        c.isalnum() or c in "-_" for c in name
    )


# ---------------------------------------------------------------- atendimento
def handle_client(conn: socket.socket, addr):
    """Roda em uma thread própria: atende UM agente do hello até a desconexão."""
    host = None
    conn.settimeout(CLIENT_TIMEOUT)
    reader = LineReader(conn)
    try:
        # 1) Handshake: a primeira mensagem TEM que ser hello com token válido.
        hello = reader.read_message()
        if hello is None:
            return
        if hello.get("type") != "hello" or not valid_hostname(hello.get("host")):
            conn.sendall(encode({"type": "error", "reason": "esperado hello válido"}))
            log.warning("%s:%d handshake inválido: %r", *addr, hello)
            return
        # compare_digest compara em tempo constante (evita ataque de temporização)
        if not hmac.compare_digest(str(hello.get("token", "")), TOKEN):
            conn.sendall(encode({"type": "error", "reason": "token inválido"}))
            log.warning("%s:%d AUTENTICAÇÃO RECUSADA (host=%s)", *addr, hello.get("host"))
            return

        host = hello["host"]
        threading.current_thread().name = f"agent-{host}"
        with hosts_lock:
            hosts[host] = {"addr": f"{addr[0]}:{addr[1]}", "online": True,
                           "last_seen": time.time(), "last": None}
        conn.sendall(encode({"type": "welcome", "version": PROTOCOL_VERSION,
                             "server_time": time.time()}))
        log.info("agente %s conectado de %s:%d", host, *addr)

        # 2) Loop principal: ping/pong e métricas.
        while True:
            msg = reader.read_message()
            if msg is None:
                log.info("agente %s encerrou a conexão", host)
                return
            mtype = msg.get("type")
            if mtype == "ping":
                conn.sendall(encode({"type": "pong", "t": msg.get("t")}))
            elif mtype == "metrics":
                try:
                    m = validate_metrics(msg)
                except ProtocolError as exc:
                    log.warning("métrica descartada de %s: %s", host, exc)
                    continue  # descarta só essa mensagem, mantém a conexão
                with db_lock:
                    db.execute(
                        "INSERT INTO metrics (host, ts, cpu, mem, disk, net_tx, net_rx, procs, rtt_ms)"
                        " VALUES (?,?,?,?,?,?,?,?,?)",
                        (host, m["ts"], m["cpu"], m["mem"], m["disk"],
                         m["net_tx"], m["net_rx"], m["procs"], m["rtt_ms"]),
                    )
                    db.commit()
                with hosts_lock:
                    hosts[host].update(last_seen=time.time(), last=m)
                log.info("%s cpu=%.1f%% mem=%.1f%% disk=%.1f%% rtt=%sms",
                         host, m["cpu"], m["mem"], m["disk"],
                         f"{m['rtt_ms']:.2f}" if m["rtt_ms"] is not None else "-")
            else:
                log.warning("tipo desconhecido de %s: %r", host, mtype)

    except socket.timeout:
        log.warning("agente %s sem resposta há %.0fs — considerado OFFLINE", host or addr, CLIENT_TIMEOUT)
    except ProtocolError as exc:
        log.warning("violação de protocolo de %s: %s", host or addr, exc)
    except (ConnectionResetError, BrokenPipeError) as exc:
        log.warning("conexão com %s perdida: %s", host or addr, exc)
    finally:
        if host:
            with hosts_lock:
                hosts[host]["online"] = False
        conn.close()
        client_slots.release()


def status_reporter():
    """Thread de fundo: a cada 10 s escreve no log um resumo de quem está online."""
    while True:
        time.sleep(10)
        with hosts_lock:
            online = sorted(h for h, s in hosts.items() if s["online"])
            offline = sorted(h for h, s in hosts.items() if not s["online"])
        log.info("RESUMO online=%s offline=%s threads_ativas=%d",
                 online, offline, threading.active_count())


def _on_sigterm(signum, frame):
    """docker stop / systemctl stop mandam SIGTERM: encerramos de forma limpa."""
    raise KeyboardInterrupt


def main():
    signal.signal(signal.SIGTERM, _on_sigterm)
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    # SO_REUSEADDR: permite reiniciar o servidor sem esperar o TIME_WAIT da porta
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((HOST, PORT))
    srv.listen(MAX_CLIENTS)
    log.info("coletor LANWatch ouvindo em %s:%d (pid=%d)", HOST, PORT, os.getpid())

    threading.Thread(target=status_reporter, name="status", daemon=True).start()

    try:
        while True:
            conn, addr = srv.accept()
            if not client_slots.acquire(blocking=False):
                log.warning("limite de %d agentes atingido, recusando %s:%d", MAX_CLIENTS, *addr)
                conn.close()
                continue
            t = threading.Thread(target=handle_client, args=(conn, addr), daemon=True)
            t.start()
    except KeyboardInterrupt:
        log.info("encerrando coletor")
    finally:
        srv.close()
        db.close()


if __name__ == "__main__":
    main()
