"""
protocol.py — Protocolo de aplicação do LANWatch (compartilhado entre coletor e agentes).

Formato: JSON Lines (NDJSON) sobre TCP.
  - Cada mensagem é UM objeto JSON codificado em UTF-8, terminado por '\n'.
  - O '\n' resolve o problema de "framing": o TCP entrega um FLUXO de bytes,
    não mensagens. Um recv() pode trazer meia mensagem ou três mensagens juntas.
    Por isso acumulamos bytes num buffer e só processamos quando achamos '\n'.

Tipos de mensagem (campo "type"):
  agente -> coletor : hello, ping, metrics
  coletor -> agente : welcome, pong, error
"""

import json

PROTOCOL_VERSION = 1
MAX_LINE_BYTES = 64 * 1024  # proteção: uma linha maior que 64 KB é descartada (entrada inválida)


class ProtocolError(Exception):
    """Mensagem malformada ou que viola o protocolo."""


def encode(msg: dict) -> bytes:
    """Converte um dict em bytes prontos para socket.sendall()."""
    return (json.dumps(msg, separators=(",", ":")) + "\n").encode("utf-8")


class LineReader:
    """
    Lê mensagens completas (uma por linha) de um socket TCP.

    Guarda os bytes que sobraram de um recv() para o próximo, porque o TCP
    não respeita os limites das nossas mensagens.
    """

    def __init__(self, sock):
        self.sock = sock
        self.buffer = b""

    def read_message(self) -> dict | None:
        """
        Retorna o próximo objeto JSON, ou None se o outro lado fechou a conexão.
        Pode lançar socket.timeout (sem dados no prazo) ou ProtocolError.
        """
        while b"\n" not in self.buffer:
            if len(self.buffer) > MAX_LINE_BYTES:
                raise ProtocolError("linha excede o tamanho máximo")
            chunk = self.sock.recv(4096)
            if not chunk:  # recv() devolve b"" quando o par fecha a conexão (FIN)
                return None
            self.buffer += chunk

        line, self.buffer = self.buffer.split(b"\n", 1)
        try:
            msg = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProtocolError(f"JSON inválido: {exc}") from exc
        if not isinstance(msg, dict) or "type" not in msg:
            raise ProtocolError("mensagem sem campo 'type'")
        return msg
