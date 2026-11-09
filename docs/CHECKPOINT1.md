# LANWatch — Checkpoint 1 (09/11/2026)

PSI129A – Conectividade em Sistemas Ciberfísicos · Prof. Luis Gonzaga de Paulo
Equipe: _[nomes e matrículas]_

---

## 1. Tema

**Projeto aplicado nº 9 — Coletor de métricas de servidores**, adaptado para uma **LAN house / arena de e-sports**.

Cada PC roda um **agente Python** que lê CPU, memória, disco, rede e processos do SO (psutil) e envia, por **TCP**, a um **coletor central** multithread. O coletor guarda tudo em SQLite e detecta na hora quando uma máquina cai ou trava. No CP2 entram o painel web (HTTP/Flask) e o alerta por e-mail (SMTP).

## 2. Cenário de uso (5.1)

**Quem usa:** o administrador de uma LAN house / arena de e-sports em Curitiba com cerca de 30 PCs gamer, que também recebe campeonatos amadores (Valorant, CS2, LoL).

**Problema:** hoje ele só descobre que um PC travou, ficou sem disco ou está com ping alto quando o jogador reclama, às vezes no meio de uma partida de campeonato. Não existe visão central do estado das máquinas.

**Resultado esperado:** um painel com o estado de cada PC em tempo real (online/offline, CPU, RAM, disco, latência até o servidor), histórico salvo e alerta por e-mail quando um PC cai ou passa de um limite, tudo sem depender de serviço externo.

## 3. Alinhamento com os RAs

| RA | Indicador | Onde aparece no projeto |
|---|---|---|
| RA1 – funções do SO | ID1.1 / ID1.2 | Agente lê `/proc` via psutil (CPU, memória, processos, disco); coletor usa threads + `Lock`; diretório `/data` com permissão `750`; processos rodando como usuário sem privilégio (`lanwatch`); tratamento de `SIGTERM` |
| RA2 – TCP/IP | ID2.1 / ID2.2 / ID2.3 | Sockets TCP puros (`socket`), protocolo próprio JSON Lines, escolha justificada de TCP, thread por conexão |
| RA3 – Linux e protocolos | ID3.1 / ID3.2 | Ambiente Docker com rede e IPs fixos; diagnóstico com `ip`, `ss`, `ping`, `getent`/DNS, `tcpdump`, `ps`; HTTP (painel) e SMTP (alertas) no CP2 |

## 4. Arquitetura

```
                 Host (Docker Engine)  — porta 5000 publicada p/ PCs reais
 ┌───────────────────────────────────────────────────────────────────────┐
 │  rede "lan" (bridge definida pelo usuário) 172.28.10.0/24, gw .1       │
 │                                                                       │
 │   ┌─────────┐   ┌─────────┐   ┌─────────┐                             │
 │   │  pc01   │   │  pc02   │   │  pc03   │   agentes (psutil)          │
 │   │  .21    │   │  .22    │   │  .23    │                             │
 │   └────┬────┘   └────┬────┘   └────┬────┘                             │
 │        │ TCP 5000 (JSON Lines: hello → ping/pong → metrics)           │
 │        └─────────────┼─────────────┘                                  │
 │                ┌─────▼──────────────┐                                 │
 │                │ collector  .10     │ thread por agente               │
 │                │ :5000 ingestão     │ SQLite em /data (volume)        │
 │                │ :8080 painel (CP2) │──SMTP:1025──► mailpit .30 (CP2)  │
 │                └────────────────────┘                                 │
 └───────────────────────────────────────────────────────────────────────┘
        ▲  PC real de cada integrante (WSL 2 / Linux) → host:5000  (CP2)
```

## 5. Plano de rede (5.3)

| Nó | Tipo | SO / imagem | IP / máscara | Gateway | Portas | Função |
|---|---|---|---|---|---|---|
| collector | contêiner | Debian 12 · python:3.12-slim | 172.28.10.10/24 | 172.28.10.1 | 5000/tcp (ingestão), 8080/tcp (painel, CP2) | coletor central |
| pc01 | contêiner | Debian 12 · python:3.12-slim | 172.28.10.21/24 | 172.28.10.1 | porta efêmera → 5000 | agente |
| pc02 | contêiner | idem | 172.28.10.22/24 | 172.28.10.1 | porta efêmera → 5000 | agente |
| pc03 | contêiner | idem | 172.28.10.23/24 | 172.28.10.1 | porta efêmera → 5000 | agente |
| mailpit (CP2) | contêiner | axllent/mailpit | 172.28.10.30/24 | 172.28.10.1 | 1025/tcp SMTP, 8025/tcp web | servidor SMTP de teste |
| DNS | embutido | Docker | 127.0.0.11 | — | 53/udp | resolve `collector` → .10 |

Faixa reservada: `.10–.19` serviços, `.20–.99` agentes, `.100+` testes avulsos.

## 6. Protocolo de aplicação (JSON Lines sobre TCP)

Uma mensagem = um objeto JSON + `\n`. O `\n` faz o *framing*, porque o TCP entrega um fluxo de bytes e não mensagens separadas.

```
agente                                coletor
  │── {"type":"hello","host":"pc01","token":"…","version":1} ──►│ valida token (compare_digest)
  │◄── {"type":"welcome","version":1,"server_time":…} ──────────│   ou {"type":"error","reason":…} e fecha
  │                                                              │
  │── {"type":"ping","t":563.71} ───────────────────────────────►│ a cada 2 s
  │◄── {"type":"pong","t":563.71} ───────────────────────────────│ → agente calcula RTT
  │── {"type":"metrics","host":"pc01","ts":…,"cpu":5.8,"mem":7.5,│
  │     "disk":29.5,"net_tx":8730,"net_rx":6998,"procs":1,       │ valida faixas → SQLite
  │     "rtt_ms":0.332} ────────────────────────────────────────►│
```

### Por que TCP (5.4)

- **Confiabilidade e ordem:** uma métrica perdida ou fora de ordem gera gráfico errado e alerta falso. O TCP garante entrega e ordem sem a gente reimplementar ACK.
- **A conexão serve de sinal de vida:** se o PC desliga, o coletor recebe FIN/RST; se ele trava, o `settimeout(15)` estoura. Nos dois casos o PC é marcado como OFFLINE na hora. Com UDP precisaríamos inventar heartbeat e detecção de perda.
- **O custo é pequeno:** cerca de 150 bytes a cada 2 s por PC. O overhead do TCP não importa nesse volume, e uma conexão persistente evita repetir o handshake.
- **Quando UDP seria melhor:** milhares de sensores mandando leituras em que perder uma não faz falta (telemetria descartável). Não é o nosso caso.

## 7. Bibliotecas

| Biblioteca | Onde | Função / justificativa |
|---|---|---|
| `socket` | ambos | sockets TCP de baixo nível; o objetivo da disciplina é ver o `bind/listen/accept/connect` explicitamente |
| `threading` | coletor | uma thread por agente + `Lock` para o estado compartilhado + `BoundedSemaphore` para limitar a 50 agentes |
| `psutil` 7.2 | agente | lê CPU, memória, disco, rede e processos do SO de forma portátil (Linux/Windows) |
| `sqlite3` | coletor | histórico de métricas num arquivo, sem precisar de um servidor de banco |
| `json` | ambos | serialização das mensagens (legível no tcpdump/Wireshark) |
| `hmac` | coletor | `compare_digest` compara o token em tempo constante |
| `logging` (+`RotatingFileHandler`) | ambos | logs com thread e horário; arquivo rotativo de 1 MB |
| `signal` | ambos | encerramento limpo no `SIGTERM` (`docker stop`, `systemctl stop`) |
| `Flask` (CP2) | coletor | painel web HTTP e API `/api/hosts` |
| `smtplib` (CP2) | coletor | envio de alertas por e-mail |

## 8. Testes já executados no CP1

Saídas completas em `docs/evidencias/`.

| ID | Objetivo | Procedimento | Esperado | Obtido | Evidência |
|---|---|---|---|---|---|
| T00 | Rede e endereçamento | `ip addr`, `ss -tnp`, `ping`, `getent hosts` | IPs do plano, 3 conexões ESTABLISHED | coletor em .10/24; 3 conexões de .21/.22/.23 na :5000; ping de 0,06 ms; DNS resolve `collector` | T00 |
| T01 | 3 clientes simultâneos | `docker compose up` | coletor atende os 3 sem bloqueio | 3 threads de agente + main + status (`NLWP=5`); cerca de 130 métricas por host gravadas; RTT médio de 0,47 ms | T01 |
| T02 | Servidor fora do ar | `docker stop collector`, esperar 12 s, `start` | agente trata o erro e reconecta | agente registrou o fechamento, depois **falha de DNS** (contêiner parado sai do DNS do Docker) com backoff de 1→2→4→8 s; reconectou em cerca de 15 s | T02 |
| T03 | Conexão recusada | agente apontado para a porta 5999 (sem nada ouvindo) | `ConnectionRefusedError` tratado | erro tratado; backoff exponencial limitado a 30 s | T03 |
| T04 | Token inválido | agente com `AGENT_TOKEN=token-errado` | coletor recusa e loga | "AUTENTICAÇÃO RECUSADA" no coletor; o agente espera 30 s antes de tentar de novo | T04 |
| T05 | Entrada inválida | cliente enviando `cpu=250`, `cpu="DROP TABLE"` e texto que não é JSON | descartar e não derrubar o servidor | as 2 métricas foram descartadas com a conexão mantida; o lixo não-JSON fechou só aquela conexão; os demais agentes não foram afetados | T05 |
| T06 | PC congelado | `docker pause pc03` por 18 s | OFFLINE após 15 s | "sem resposta há 15s — considerado OFFLINE"; voltou a ONLINE sozinho após o `unpause` | T06 |
| T07 | Encerramento limpo | `docker stop collector` | sair rápido pelo SIGTERM | 0,15 s (antes do tratamento levava 10 s até o SIGKILL) | T07 |
| T08 | Formato na rede | `tcpdump -A` na :5000 | ver ping/pong/metrics | mensagens JSON visíveis em texto puro, o que mostra a necessidade de TLS (CP2) | T08 |

## 9. Limitações percebidas (e o que fazer)

1. **Token em texto puro na rede:** o tcpdump (T08) mostra o `hello` com o token. Plano para o CP2: TLS com o módulo `ssl` e certificado autoassinado.
2. **Métricas de contêiner refletem o host:** `/proc/stat` e `/proc/meminfo` dentro do contêiner são do host, então pc01–03 mostram valores parecidos, e `procs=1` porque cada contêiner tem seu próprio PID namespace. Plano: cada integrante roda o agente no próprio PC (WSL 2/Linux), apontando para `IP_DO_HOST:5000`. Isso dá máquinas reais e diferentes, que é o cenário de verdade.
3. **SQLite com um lock global:** basta para cerca de 30 PCs a cada 2 s (cerca de 15 inserts/s). Para centenas de PCs: inserts em lote ou outro banco.

## 10. Plano até o Checkpoint 2 (23/11) — semana de prova no dia 16

| Entrega | Requisito | Responsável sugerido |
|---|---|---|
| Painel web Flask (`/` + `/api/hosts`) servido em thread separada na :8080 | 5.6 HTTP | dev Python |
| Alertas SMTP (PC offline / CPU > 90% / disco > 90% / RTT > 50 ms) via Mailpit | 5.6 SMTP | dev Python |
| TLS no canal agente→coletor | 5.9 | infra |
| Firewall: `ufw` na VM/WSL liberando só 5000/8080 | 5.9 / 5.7 | infra |
| Agente como serviço `systemd` numa VM/WSL + `journalctl` | 5.7 | infra |
| Teste com perda e atraso (`tc netem`) mostrando o RTT subindo e o alerta disparando | 5.7 / 5.8 | testes |
| Roteiro do vídeo (10 min, todos falam) | 11 / 12 | documentação |

## 11. Papéis sugeridos (seção 3 do enunciado)

- **Infraestrutura e redes:** compose, plano de rede, firewall, systemd
- **Desenvolvimento Python:** coletor, agente, painel, alertas
- **Testes e evidências:** casos de teste, tcpdump/Wireshark, prints
- **Documentação e vídeo:** documento-resumo, roteiro, edição

Lembrete do enunciado: **todo mundo precisa saber explicar qualquer parte** (pode haver arguição).
