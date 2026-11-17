"""Cliente do painel central: monta o retrato das sessões desta máquina e envia ao servidor.

Cada envio é um retrato completo (sessões vivas, eventos das últimas 48 h e anomalias): o
servidor substitui o estado da máquina, então reenviar é inofensivo e um envio perdido se
corrige sozinho no seguinte.
"""

from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import __version__
from .banco import Banco

VERSAO_ESQUEMA = 1
JANELA_EVENTOS = 48 * 3600
LIMITE_MSG = 300
TIMEOUT = 5
STATUS = {"busy": "trabalhando", "idle": "parada", "waiting": "parada"}
"""Status do provedor → o que o painel mostra. Desconhecido vira None (não sabemos)."""


@dataclass(frozen=True)
class Config:
    url: str
    token: str
    maquina: str


class ErroEnvio(Exception):
    pass


def arquivo_config() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "llm-sessoes" / "central.env"


def caminho_log() -> Path:
    """O mesmo arquivo para onde o cron manda a saída do `coletar`."""
    return Path.home() / ".local/state/llm-sessoes/coletor.log"


def carregar_config(ambiente: dict[str, str] | None = None,
                    arquivo: Path | None = None) -> Config | None:
    """Variáveis de ambiente têm precedência; o arquivo `central.env` é o fallback.

    Devolve None quando não há URL: o painel central é opcional.
    """
    ambiente = os.environ if ambiente is None else ambiente
    do_arquivo = _ler_env(arquivo or arquivo_config())

    def valor(chave: str) -> str:
        return (ambiente.get(chave) or do_arquivo.get(chave) or "").strip()

    url = valor("LLM_SESSOES_CENTRAL_URL").rstrip("/")
    if not url:
        return None
    return Config(url=url, token=valor("LLM_SESSOES_TOKEN"),
                  maquina=valor("LLM_SESSOES_MAQUINA") or socket.gethostname())


def montar(banco: Banco, maquina: str, boot_id: str, agora: int) -> dict:
    """O retrato que vai para `POST /api/v1/relatorio` (versao_esquema 1)."""
    vivas = [_sessao(s, inicio=s["desde"], status=STATUS.get(s["status"] or ""),
                     tmux=_json_ou_none(s["tmux"]))
             for s in banco.vivas()]
    eventos = [{"tipo": e["tipo"], "quando": e["quando"],
                **_sessao(e, inicio=e["primeira_vez"], status=None, tmux=None)}
               for e in banco.eventos_desde(agora - JANELA_EVENTOS)]
    return {
        "versao_esquema": VERSAO_ESQUEMA,
        "maquina": maquina,
        "boot_id": boot_id,
        "coletado_em": agora,
        "versao_cliente": __version__,
        "vivas": vivas,
        "eventos": eventos,
        "anomalias": json.loads(banco.meta("avisos") or "[]"),
    }


def enviar(config: Config, payload: dict, timeout: float = TIMEOUT) -> None:
    """Levanta ErroEnvio com uma descrição curta em qualquer falha (rede, HTTP, URL)."""
    corpo = json.dumps(payload, ensure_ascii=False).encode()
    pedido = urllib.request.Request(
        f"{config.url}/api/v1/relatorio", data=corpo, method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {config.token}",
                 "User-Agent": f"llm-sessoes/{__version__}"})
    try:
        with urllib.request.urlopen(pedido, timeout=timeout) as resposta:
            resposta.read()
    except urllib.error.HTTPError as erro:
        raise ErroEnvio(f"HTTP {erro.code} {erro.reason}") from None
    except urllib.error.URLError as erro:
        raise ErroEnvio(f"sem conexão: {erro.reason}") from None
    except (OSError, ValueError) as erro:  # timeout de leitura, URL malformada…
        raise ErroEnvio(f"{type(erro).__name__}: {erro}") from None


def enviar_do_banco(banco: Banco, boot_id: str, agora: int,
                    config: Config | None = None) -> bool | None:
    """Envia o retrato atual. None = sem configuração; False = falhou (anotado no log).

    Nunca levanta exceção: roda no fim da coleta e do hook, que não podem cair por causa da rede.
    """
    try:
        config = config or carregar_config()
        if config is None:
            return None
        enviar(config, montar(banco, config.maquina, boot_id, agora))
        return True
    except Exception as erro:
        motivo = str(erro) if isinstance(erro, ErroEnvio) else repr(erro)
        anotar_no_log(f"enviar ao painel central falhou: {motivo}")
        return False


def anotar_no_log(mensagem: str) -> None:
    try:
        log = caminho_log()
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a") as f:
            f.write(f"{datetime.now():%d/%m %H:%M} {mensagem}\n")
    except OSError:
        pass


def _sessao(s, inicio: int | None, status: str | None, tmux: dict | None) -> dict:
    msg = s["ultima_msg"]
    return {
        "provedor": s["provedor"],
        "id": s["session_id"],
        "titulo": s["titulo"],
        "ultima_msg": msg[:LIMITE_MSG] if msg else None,
        "dir_resume": s["dir_resume"],
        "dir_trabalho": s["dir_trabalho"],
        "branch": s["branch"],
        "status": status,
        "inicio": inicio,
        "ultima_atividade": s["ultima_atividade"],
        "tmux": tmux,
    }


def _json_ou_none(texto: str | None):
    try:
        return json.loads(texto) if texto else None
    except ValueError:
        return None


def _ler_env(arquivo: Path) -> dict[str, str]:
    """`CHAVE=valor` por linha; aceita `export`, comentários e aspas em volta do valor."""
    try:
        linhas = arquivo.read_text().splitlines()
    except OSError:
        return {}
    valores = {}
    for linha in linhas:
        linha = linha.strip()
        if not linha or linha.startswith("#") or "=" not in linha:
            continue
        chave, _, valor = linha.removeprefix("export ").partition("=")
        valor = valor.strip()
        if len(valor) >= 2 and valor[0] == valor[-1] and valor[0] in "'\"":
            valor = valor[1:-1]
        valores[chave.strip()] = valor
    return valores
