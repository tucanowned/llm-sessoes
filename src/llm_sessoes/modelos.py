from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SessaoViva:
    """Uma sessão de LLM com processo vivo no momento da coleta."""

    provedor: str
    session_id: str
    pid: int
    dir_resume: str
    """Diretório onde o processo foi iniciado — de onde o resume precisa rodar."""
    dir_trabalho: str | None = None
    """Diretório onde a sessão está trabalhando de fato (ex.: um worktree)."""
    tty: str | None = None
    status: str | None = None
    titulo: str | None = None
    branch: str | None = None
    ultima_msg: str | None = None
    versao: str | None = None
    """Versão da ferramenta — serve para correlacionar quebras com atualizações."""

    @property
    def chave(self) -> tuple[str, str]:
        return (self.provedor, self.session_id)
