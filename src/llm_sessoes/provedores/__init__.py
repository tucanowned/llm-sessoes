from .claude import ProvedorClaude
from .codex import ProvedorCodex


def todos():
    return [ProvedorClaude(), ProvedorCodex()]


def por_nome(nome: str):
    return {p.nome: p for p in todos()}[nome]
