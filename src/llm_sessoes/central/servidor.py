"""HTTP do painel central (ThreadingHTTPServer, só stdlib).

Rotas:
    POST /api/v1/relatorio   retrato de uma máquina (Authorization: Bearer <token>)
    GET  /healthz            200 "ok"
    GET  /api/v1/resumo      {ativas, maquinas_online, maquinas_total, crashes_24h}
    GET  /                   a página
"""

from __future__ import annotations

import hmac
import json
import secrets
import signal
import sys
import threading
import time
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import pagina
from .armazem import Armazem, RelatorioInvalido, validar

LIMITE_CORPO = 1024 * 1024


class Servidor(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, endereco: tuple[str, int], armazem: Armazem, tokens: list[str],
                 relogio: Callable[[], float] = time.time):
        self.armazem = armazem
        self.tokens = [t for t in tokens if t]
        self.relogio = relogio
        # Uma conexão SQLite compartilhada: as escritas são raras (uma por máquina a cada 2 min)
        # e o lock evita "database is locked" entre threads do próprio servidor.
        self.lock = threading.Lock()
        super().__init__(endereco, Handler)

    def agora(self) -> int:
        return int(self.relogio())

    def token_valido(self, token: str) -> bool:
        # compare_digest com todos, sem parar no primeiro: não vaza qual token chegou perto.
        return any([hmac.compare_digest(token.encode(), t.encode()) for t in self.tokens])


class Handler(BaseHTTPRequestHandler):
    server: Servidor
    server_version = "llm-sessoes-central"
    sys_version = ""

    # --- rotas ---------------------------------------------------------------------------

    def do_GET(self) -> None:
        caminho = self.path.split("?", 1)[0]
        if caminho == "/healthz":
            self._responder(HTTPStatus.OK, b"ok\n", "text/plain; charset=utf-8")
        elif caminho == "/api/v1/resumo":
            with self.server.lock:
                resumo = self.server.armazem.resumo(self.server.agora())
            self._json(HTTPStatus.OK, resumo)
        elif caminho == "/":
            agora = self.server.agora()
            with self.server.lock:
                maquinas = self.server.armazem.painel(agora)
                resumo = self.server.armazem.resumo(agora)
            nonce = secrets.token_urlsafe(16)
            aba = parse_qs(urlsplit(self.path).query).get("aba", [None])[0]
            html = pagina.renderizar(maquinas, resumo, agora, nonce, aba)
            self._responder(HTTPStatus.OK, html.encode(), "text/html; charset=utf-8", {
                "Content-Security-Policy": (f"default-src 'none'; style-src 'nonce-{nonce}'; "
                                            f"script-src 'nonce-{nonce}'; base-uri 'none'; "
                                            "form-action 'none'; frame-ancestors 'none'"),
                "Cache-Control": "no-store",
            })
        else:
            self._erro(HTTPStatus.NOT_FOUND, "rota desconhecida")

    def do_POST(self) -> None:
        if self.path.split("?", 1)[0] != "/api/v1/relatorio":
            self._erro(HTTPStatus.NOT_FOUND, "rota desconhecida")
            return

        autorizacao = self.headers.get("Authorization", "")
        esquema, _, token = autorizacao.partition(" ")
        if esquema.lower() != "bearer" or not self.server.token_valido(token.strip()):
            self._erro(HTTPStatus.UNAUTHORIZED, "token ausente ou inválido",
                       {"WWW-Authenticate": "Bearer"})
            return

        tamanho = self.headers.get("Content-Length")
        if tamanho is None:
            self._erro(HTTPStatus.LENGTH_REQUIRED, "Content-Length obrigatório")
            return
        try:
            tamanho = int(tamanho)
            if tamanho < 0:
                raise ValueError
        except ValueError:
            self._erro(HTTPStatus.BAD_REQUEST, "Content-Length inválido")
            return
        if tamanho > LIMITE_CORPO:
            # Não lê o corpo: responde e fecha a conexão.
            self._erro(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, f"corpo acima de {LIMITE_CORPO} bytes")
            return

        try:
            relatorio = validar(json.loads(self.rfile.read(tamanho)))
        except (ValueError, UnicodeDecodeError) as erro:  # inclui RelatorioInvalido e JSON ruim
            motivo = str(erro) if isinstance(erro, RelatorioInvalido) else "JSON inválido"
            self._erro(HTTPStatus.BAD_REQUEST, motivo)
            return

        with self.server.lock:
            self.server.armazem.gravar(relatorio, self.server.agora())
        self._json(HTTPStatus.OK, {"ok": True, "maquina": relatorio["maquina"],
                                   "vivas": len(relatorio["vivas"])})

    # --- respostas -----------------------------------------------------------------------

    def _json(self, status: HTTPStatus, dado: dict, extra: dict | None = None) -> None:
        corpo = json.dumps(dado, ensure_ascii=False).encode()
        self._responder(status, corpo, "application/json; charset=utf-8", extra)

    def _erro(self, status: HTTPStatus, motivo: str, extra: dict | None = None) -> None:
        self.close_connection = True  # o corpo pode não ter sido lido
        self._json(status, {"erro": motivo}, {"Connection": "close", **(extra or {})})

    def _responder(self, status: HTTPStatus, corpo: bytes, tipo: str,
                   extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(corpo)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for chave, valor in (extra or {}).items():
            self.send_header(chave, valor)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(corpo)

    def log_message(self, formato: str, *args) -> None:
        if self.path.startswith("/healthz"):
            return  # o healthcheck do Docker roda a cada 30 s e só polui o log
        sys.stderr.write(f"{self.address_string()} {formato % args}\n")


def rodar(endereco: str, porta: int, caminho_banco: Path | str, tokens: list[str]) -> None:
    servidor = Servidor((endereco, porta), Armazem(caminho_banco), tokens)
    print(f"llm-sessoes central em http://{endereco}:{porta} (banco {caminho_banco}, "
          f"{len(servidor.tokens)} token(s))", file=sys.stderr, flush=True)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # `docker stop` encerra limpo
    try:
        servidor.serve_forever()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        servidor.server_close()
        servidor.armazem.fechar()
