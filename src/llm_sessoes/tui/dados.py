"""O que a TUI mostra: itens por aba e a busca. Sem Textual — só banco e stdlib."""

from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from ..banco import Banco

ABAS = ("abertas", "fechadas", "crash", "todas")
JANELAS_FECHADAS = (24, 72, 168)
"""Horas — alternadas com `h` na aba Fechadas."""

ABERTA, FECHADA, PENDENTE, REABERTA, CRASH, DESCONHECIDO = (
    "aberta", "fechada", "PENDENTE", "reaberta", "crash", "?")


@dataclass(frozen=True)
class Item:
    provedor: str
    session_id: str
    titulo: str | None
    dir_resume: str
    dir_trabalho: str | None
    branch: str | None
    ultima_msg: str | None
    estado: str
    quando: int
    """Aberta: desde quando. Fechada/crash: quando aconteceu. Demais: última vez vista."""
    tty: str | None = None

    @property
    def chave(self) -> str:
        return f"{self.provedor}:{self.session_id}"

    @property
    def viva(self) -> bool:
        return self.estado in (ABERTA, REABERTA)

    @property
    def em_worktree(self) -> bool:
        """Trabalha fora do diretório de resume, ou o próprio resume já é um worktree."""
        trabalho = self.dir_trabalho or self.dir_resume
        return trabalho != self.dir_resume or "/worktrees/" in trabalho + "/"

    @property
    def diretorio(self) -> str:
        """Nome curto do diretório onde a sessão trabalha."""
        return Path(self.dir_trabalho or self.dir_resume).name

    def como_sessao(self) -> dict:
        """No formato que `reabrir.reabrir` espera."""
        return {"provedor": self.provedor, "session_id": self.session_id,
                "titulo": self.titulo, "dir_resume": self.dir_resume}


@dataclass(frozen=True)
class Retrato:
    """Tudo que a tela precisa, lido do banco de uma vez."""

    itens: dict[str, list[Item]]
    ultima_coleta: int | None
    avisos: list[str]


def carregar(banco: Banco, agora: int, horas_fechadas: float = JANELAS_FECHADAS[0]) -> Retrato:
    abertas = [_item(s, ABERTA, s["desde"]) for s in banco.vivas()]
    fechadas = [_item(s, FECHADA, s["quando"])
                for s in banco.fechadas(desde=int(agora - horas_fechadas * 3600))]
    crash = [_item(s, REABERTA if s["reaberta"] else PENDENTE, s["quando"])
             for s in banco.ultimo_crash()]
    crash.sort(key=lambda i: i.estado != PENDENTE)  # pendentes primeiro; o sort é estável
    todas = [_item(s, _estado_geral(s), s["ultima_vez"]) for s in banco.todas()]
    ultima = banco.meta("ultima_coleta")
    return Retrato(
        itens={"abertas": abertas, "fechadas": fechadas, "crash": crash, "todas": todas},
        ultima_coleta=int(ultima) if ultima else None,
        avisos=json.loads(banco.meta("avisos") or "[]"),
    )


def filtrar(itens: list[Item], consulta: str) -> list[Item]:
    """Todos os termos precisam aparecer (em título, diretórios, branch ou provedor)."""
    termos = normalizar(consulta).split()
    if not termos:
        return list(itens)
    return [i for i in itens if all(t in _palheiro(i) for t in termos)]


def ha_quanto(quando: int, agora: int) -> str:
    """Tempo relativo curto: o que interessa na lista é "faz quanto", não o relógio."""
    segundos = max(0, agora - quando)
    if segundos < 90:
        return "agora"
    if segundos < 3600:
        return f"há {segundos // 60} min"
    if segundos < 48 * 3600:
        return f"há {segundos // 3600} h"
    return f"há {segundos // 86400} d"


def normalizar(texto: str) -> str:
    sem_acento = "".join(c for c in unicodedata.normalize("NFKD", texto)
                         if not unicodedata.combining(c))
    return sem_acento.casefold()


def _palheiro(i: Item) -> str:
    return normalizar(" ".join(filter(None, (i.titulo, i.dir_resume, i.dir_trabalho,
                                             i.branch, i.provedor))))


def _estado_geral(s) -> str:
    if s["viva"]:
        return ABERTA
    return {"fechou": FECHADA, "crash": CRASH}.get(s["ultimo_evento"], DESCONHECIDO)


def _item(s, estado: str, quando: int) -> Item:
    return Item(
        provedor=s["provedor"], session_id=s["session_id"], titulo=s["titulo"],
        dir_resume=s["dir_resume"], dir_trabalho=s["dir_trabalho"], branch=s["branch"],
        ultima_msg=s["ultima_msg"], estado=estado, quando=quando,
        tty=s["tty"] if "tty" in s.keys() else None,
    )
