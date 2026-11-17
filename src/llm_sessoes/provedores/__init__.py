from .claude import ProvedorClaude
from .codex import ProvedorCodex
from .opencode import ProvedorOpencode


def todos():
    return [ProvedorClaude(), ProvedorCodex(), ProvedorOpencode()]


def por_nome(nome: str):
    return {p.nome: p for p in todos()}[nome]
