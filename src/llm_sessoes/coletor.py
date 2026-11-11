"""Uma rodada de coleta: compara quem estava vivo na rodada anterior com quem está vivo agora."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Iterable, Protocol

from .banco import Banco
from .modelos import SessaoViva

RETENCAO_EVENTOS = 90 * 24 * 3600


class Provedor(Protocol):
    nome: str

    def sessoes_vivas(self) -> list[SessaoViva]: ...


@dataclass
class Resultado:
    abriram: list[tuple[str, str]] = field(default_factory=list)
    fecharam: list[tuple[str, str]] = field(default_factory=list)
    crash: list[tuple[str, str]] = field(default_factory=list)
    vivas: int = 0
    avisos: list[str] = field(default_factory=list)
    notas: list[str] = field(default_factory=list)
    """Informativo (ex.: versão nova de uma ferramenta) — não indica problema."""


def coletar(banco: Banco, provedores: Iterable[Provedor], agora: int, boot_id: str) -> Resultado:
    resultado = Resultado()

    atuais: dict[tuple[str, str], SessaoViva] = {}
    provedores_ok: set[str] = set()
    for provedor in provedores:
        try:
            for sessao in provedor.sessoes_vivas():
                atuais[sessao.chave] = sessao
            # Anomalia (processo vivo que o provedor não soube ler) costuma ser formato interno
            # que mudou numa atualização: a lista pode estar incompleta, então não é confiável
            # para concluir que algo fechou.
            anomalias = list(getattr(provedor, "avisos", []))
            if anomalias:
                resultado.avisos.extend(f"{provedor.nome}: {a}" for a in anomalias)
            else:
                provedores_ok.add(provedor.nome)
        except Exception as erro:  # um provedor quebrado não pode derrubar a coleta dos outros
            resultado.avisos.append(f"provedor {provedor.nome} falhou: {erro!r}")

    with banco.con:  # transação única: ou a rodada inteira entra, ou nada
        boot_anterior = banco.meta("boot_id")
        ultima_coleta = int(banco.meta("ultima_coleta") or agora)
        anteriores = {(v["provedor"], v["session_id"]): v for v in banco.vivas()}

        if boot_anterior and boot_anterior != boot_id:
            # A máquina reiniciou: tudo que estava vivo morreu sem se despedir. O evento é datado
            # da última coleta — o último momento em que sabemos que estavam abertas.
            for chave, viva in anteriores.items():
                banco.registrar_evento(ultima_coleta, "crash", *chave,
                                       viva["pid"], viva["tty"], boot_anterior)
                banco.remover_viva(*chave)
                resultado.crash.append(chave)
            anteriores = {}

        for chave, viva in anteriores.items():
            if chave in atuais:
                continue
            if chave[0] not in provedores_ok:
                continue  # provedor falhou nesta rodada: ausência não significa que fechou
            banco.registrar_evento(agora, "fechou", *chave, viva["pid"], viva["tty"], boot_id)
            banco.remover_viva(*chave)
            resultado.fecharam.append(chave)

        for chave, sessao in atuais.items():
            banco.salvar_sessao(sessao, agora)
            if chave not in anteriores:
                banco.registrar_evento(agora, "abriu", *chave, sessao.pid, sessao.tty, boot_id)
                resultado.abriram.append(chave)
            banco.salvar_viva(sessao, agora)

        _anotar_versoes(banco, atuais.values(), resultado)
        banco.definir_meta("avisos", json.dumps(resultado.avisos))
        banco.definir_meta("boot_id", boot_id)
        banco.definir_meta("ultima_coleta", str(agora))
        banco.expurgar_eventos(agora - RETENCAO_EVENTOS)

    resultado.vivas = len(atuais)
    return resultado


def registrar_fechamento(banco: Banco, provedor: str, session_id: str, agora: int,
                         boot_id: str) -> bool:
    """Fechamento avisado pela própria ferramenta (hook), sem esperar a próxima rodada.

    Só vale para sessão que o coletor já viu viva; a rodada seguinte não a encontra mais em
    `vivas` e, portanto, não duplica o evento.
    """
    with banco.con:
        viva = next((v for v in banco.vivas()
                     if (v["provedor"], v["session_id"]) == (provedor, session_id)), None)
        if viva is None:
            return False
        banco.registrar_evento(agora, "fechou", provedor, session_id, viva["pid"], viva["tty"], boot_id)
        banco.remover_viva(provedor, session_id)
    return True


def _anotar_versoes(banco: Banco, sessoes: Iterable[SessaoViva], resultado: Resultado) -> None:
    """Registra versões nunca vistas: se um provedor quebrar, o log mostra a atualização suspeita."""
    por_provedor: dict[str, set[str]] = {}
    for s in sessoes:
        if s.versao:
            por_provedor.setdefault(s.provedor, set()).add(s.versao)

    for provedor, versoes in por_provedor.items():
        chave = f"versoes_{provedor}"
        conhecidas = set(json.loads(banco.meta(chave) or "[]"))
        novas = versoes - conhecidas
        if not novas:
            continue
        if conhecidas:  # na primeira coleta tudo é "novo": não vale nota
            resultado.notas.append(f"{provedor}: versão nova em uso: {', '.join(sorted(novas))}")
        banco.definir_meta(chave, json.dumps(sorted(conhecidas | novas)))
