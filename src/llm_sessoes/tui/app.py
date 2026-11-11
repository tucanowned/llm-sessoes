from __future__ import annotations

import shlex
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.theme import Theme
from textual.widgets import DataTable, Footer, Input, Static

from .. import provedores, reabrir
from ..banco import Banco
from . import dados

ROTULOS = {"abertas": "Abertas", "fechadas": "Fechadas", "crash": "Último crash", "todas": "Todas"}
CONFIRMAR_ACIMA_DE = 5
RECARGA_SEGUNDOS = 30

# Paleta: cinza-grafite neutro de fundo (não preto — cansa menos e é o que o olho espera), âmbar de sinalização para o que pede ação,
# verde-água para o que está vivo e coral para o que caiu.
FUNDO, SUPERFICIE, PAINEL = "#262a31", "#2d323a", "#3a404b"
TEXTO, APAGADO = "#dcdfe4", "#8b93a1"
AMBAR, AGUA, CORAL, LILAS = "#f2b84b", "#5fc9b8", "#ef6b73", "#a99cf5"

TEMA = Theme(
    name="llm-sessoes", dark=True,
    primary=AMBAR, secondary=LILAS, accent=AMBAR,
    foreground=TEXTO, background=FUNDO, surface=SUPERFICIE, panel=PAINEL,
    success=AGUA, warning=AMBAR, error=CORAL,
    variables={"footer-key-foreground": AMBAR, "footer-background": FUNDO,
               "footer-description-foreground": APAGADO, "input-cursor-background": AMBAR,
               "block-cursor-background": PAINEL, "block-cursor-foreground": TEXTO,
               "block-cursor-text-style": "none"},
)

SELOS = {"claude": f"bold {FUNDO} on #e0926a", "codex": f"bold {FUNDO} on #7fb2f0"}
ESTADOS = {  # estado → (símbolo, cor)
    dados.ABERTA: ("●", AGUA), dados.REABERTA: ("●", AGUA), dados.PENDENTE: ("▲", CORAL),
    dados.CRASH: ("▲", CORAL), dados.FECHADA: ("○", AMBAR), dados.DESCONHECIDO: ("·", APAGADO),
    "abrindo…": ("↻", LILAS),
}
VAZIO = {
    "abertas": "Nenhuma sessão viva na última coleta.",
    "fechadas": "Nenhuma sessão fechada nesta janela.\nAperte h para olhar mais para trás.",
    "crash": "Nenhum crash registrado. Que continue assim.",
    "todas": "O coletor ainda não viu nenhuma sessão.\nRode: llm-sessoes coletar",
}


class Confirmar(ModalScreen[bool]):
    BINDINGS = [Binding("s,enter", "responder(True)", "Sim"),
                Binding("n,escape", "responder(False)", "Não")]
    DEFAULT_CSS = """
    Confirmar { align: center middle; background: $background 70%; }
    Confirmar > Static { width: auto; padding: 1 4; border: round $accent; background: $surface; }
    """

    def __init__(self, pergunta: str):
        super().__init__()
        self.pergunta = pergunta

    def compose(self) -> ComposeResult:
        yield Static(f"[b]{self.pergunta}[/b]\n\n[b $accent]s[/] abrir    [b $accent]n[/] cancelar")

    def action_responder(self, resposta: bool) -> None:
        self.dismiss(resposta)


class SessoesApp(App):
    TITLE = "llm-sessoes"
    ENABLE_COMMAND_PALETTE = False
    CSS = """
    Screen { background: $background; }
    #topo { height: 1; margin: 1 2 0 2; }
    #abas { height: 1; margin: 1 2 0 2; link-style: none; link-style-hover: bold;
            link-background-hover: $panel; link-color-hover: $text; }
    * { scrollbar-color: $panel; scrollbar-color-hover: $text-muted; scrollbar-color-active: $accent;
        scrollbar-background: $background; scrollbar-background-hover: $background;
        scrollbar-background-active: $background; scrollbar-size-vertical: 1; }
    #avisos { margin: 1 2 0 2; padding: 0 1; border-left: thick $warning; background: $surface;
              color: $warning; display: none; }
    #avisos.visivel { display: block; }

    #corpo { height: 1fr; margin: 1 1 0 1; }
    #lista, #painel { border: round $panel; border-title-color: $text-muted;
                      border-subtitle-color: $text-muted; background: $background; }
    #lista { width: 1fr; padding: 0 1; }
    #lista:focus-within { border: round $accent 60%; border-title-color: $accent; }
    #painel { width: 46; padding: 1 2; }
    #painel.oculto { display: none; }

    #linha-busca { height: 1; margin-bottom: 1; }
    #rotulo-busca { width: 2; color: $accent; text-style: bold; }
    #busca { border: none; height: 1; padding: 0; background: $background; }
    #busca:focus { background: $surface; }

    DataTable { background: $background; scrollbar-size-vertical: 1; }
    DataTable > .datatable--cursor { background: $panel; }
    DataTable:focus > .datatable--cursor { background: $panel; }
    DataTable > .datatable--hover { background: $surface; }
    #vazio { display: none; height: 1fr; content-align: center middle; text-align: center;
             color: $text-muted; }
    #lista.sem-itens DataTable { display: none; }
    #lista.sem-itens #vazio { display: block; }

    #comando { margin-top: 1; padding: 0 1; border: round $panel; border-title-color: $text-muted;
               color: $text-muted; }
    Footer { background: $background; }
    """
    BINDINGS = [
        Binding("enter", "reabrir", "reabrir", priority=False),
        Binding("space", "marcar", "marcar"),
        Binding("a", "marcar_todas", "todas"),
        Binding("slash", "buscar", "buscar", key_display="/"),
        Binding("1", "aba('abertas')", "abas", key_display="1-4"),
        Binding("2", "aba('fechadas')", show=False),
        Binding("3", "aba('crash')", show=False),
        Binding("4", "aba('todas')", show=False),
        Binding("tab", "aba_vizinha(1)", show=False, priority=True),
        Binding("shift+tab", "aba_vizinha(-1)", show=False, priority=True),
        Binding("h", "janela", "janela"),
        Binding("c", "copiar", "copiar"),
        Binding("d", "detalhe", "detalhe"),
        Binding("r", "recarregar", "recarregar"),
        Binding("escape", "limpar_busca", show=False),
        Binding("q", "quit", "sair"),
    ]

    def __init__(self, caminho_banco: Path | str,
                 reabrir_sessoes: Callable[[list[dict]], reabrir.Resultado] | None = None,
                 agora: Callable[[], float] = time.time):
        super().__init__()
        self.caminho_banco = caminho_banco
        self.reabrir_sessoes = reabrir_sessoes or reabrir.reabrir
        self.agora = agora
        self.aba = "fechadas"
        self.janela = 0
        self.consulta = ""
        self.marcadas: set[str] = set()
        self.reabertas_agora: set[str] = set()
        self.retrato = dados.Retrato({a: [] for a in dados.ABAS}, None, [])
        self.visiveis: list[dados.Item] = []
        self.largura_titulo = 40

    # --- montagem -----------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Static(id="topo")
        yield Static(id="abas")
        yield Static(id="avisos")
        with Horizontal(id="corpo"):
            with Vertical(id="lista"):
                with Horizontal(id="linha-busca"):
                    yield Static("/", id="rotulo-busca")
                    yield Input(id="busca", placeholder="buscar por título, diretório, branch ou provedor")
                yield DataTable(id="tabela", cursor_type="row", show_header=False, cell_padding=1)
                yield Static(id="vazio")
            with VerticalScroll(id="painel"):
                yield Static(id="detalhe")
                yield Static(id="comando")
        yield Footer()

    def on_mount(self) -> None:
        self.register_theme(TEMA)
        self.theme = TEMA.name
        self.query_one("#painel").border_title = "Detalhe"
        self.query_one("#comando").border_title = "c copia"
        tabela = self.query_one(DataTable)
        tabela.add_column("", key="marca", width=1)
        tabela.add_column("", key="selo", width=9)
        tabela.add_column("", key="titulo", width=self.largura_titulo)
        tabela.add_column("", key="quando", width=10)
        self.carregar()
        self.aba = self._aba_inicial()
        self.atualizar()
        tabela.focus()
        self.set_timer(0.1, self._ajustar_colunas)  # no 1º resize a tabela ainda não tem tamanho
        self.set_interval(RECARGA_SEGUNDOS, self.action_recarregar)

    def on_resize(self) -> None:
        self.set_timer(0.1, self._ajustar_colunas)  # depois do layout

    def _ajustar_colunas(self) -> None:
        """O título fica com a largura que sobrar das colunas fixas."""
        tabela = self.query_one(DataTable)
        fixas = sum(c.width for chave, c in tabela.columns.items() if chave.value != "titulo")
        respiro = 2 * tabela.cell_padding * len(tabela.columns) + 1  # +1: barra de rolagem
        largura = max(16, tabela.size.width - fixas - respiro)
        if tabela.size.width and largura != self.largura_titulo:
            self.largura_titulo = tabela.columns["titulo"].width = largura
            self.atualizar()  # recriar as linhas é o jeito público de invalidar o cache de render

    def _aba_inicial(self) -> str:
        """Abre onde provavelmente há o que fazer: crash pendente > fechadas > abertas."""
        if any(i.estado == dados.PENDENTE for i in self.retrato.itens["crash"]):
            return "crash"
        return "fechadas" if self.retrato.itens["fechadas"] else "abertas"

    # --- dados → tela ---------------------------------------------------------------

    def carregar(self) -> None:
        banco = Banco(self.caminho_banco)
        try:
            self.retrato = dados.carregar(banco, int(self.agora()), dados.JANELAS_FECHADAS[self.janela])
        finally:
            banco.fechar()
        vivas = {i.chave for i in self.retrato.itens["abertas"]}
        self.reabertas_agora -= vivas  # a coleta já as viu; o estado normal assume daqui

    def atualizar(self) -> None:
        """Redesenha tudo a partir de `self.retrato`, preservando o cursor."""
        tabela = self.query_one(DataTable)
        anterior = self.item_atual()
        self.visiveis = dados.filtrar(self.retrato.itens[self.aba], self.consulta)

        tabela.clear()
        for item in self.visiveis:
            tabela.add_row(*self._celulas(item), key=item.chave, height=2)
        if anterior and anterior.chave in {i.chave for i in self.visiveis}:
            tabela.move_cursor(row=tabela.get_row_index(anterior.chave))

        lista = self.query_one("#lista")
        lista.set_class(not self.visiveis, "sem-itens")
        self.query_one("#vazio", Static).update(
            f"Nada bate com “{self.consulta}”.\nesc limpa a busca." if self.consulta else VAZIO[self.aba])
        lista.border_title = self._titulo_da_lista()
        self.query_one("#topo", Static).update(self._topo())
        self.query_one("#abas", Static).update(self._barra_de_abas())
        avisos = self.query_one("#avisos", Static)
        avisos.update(Text("\n".join(
            f"⚠ {a} — fechamentos desse provedor não estão sendo registrados" for a in self.retrato.avisos)))
        avisos.set_class(bool(self.retrato.avisos), "visivel")
        self._atualizar_marcadas()
        self._atualizar_detalhe()

    def _topo(self) -> Text:
        vivas = len(self.retrato.itens["abertas"])
        pendentes = sum(i.estado == dados.PENDENTE for i in self.retrato.itens["crash"])
        coleta = (f"coletado {dados.ha_quanto(self.retrato.ultima_coleta, int(self.agora()))}"
                  if self.retrato.ultima_coleta else "nenhuma coleta ainda — rode llm-sessoes coletar")
        topo = Text.assemble(("▞▚ ", AMBAR), ("llm-sessoes", f"bold {TEXTO}"), "   ",
                             ("● ", AGUA), (f"{vivas} viva{'s' if vivas != 1 else ''}", TEXTO))
        if pendentes:
            topo.append_text(Text.assemble("   ", ("▲ ", CORAL), (
                f"{pendentes} do último crash esperando você", f"bold {CORAL}")))
        topo.append(f"   {coleta}", APAGADO)
        return topo

    def _barra_de_abas(self) -> Text:
        barra = Text()
        for n, aba in enumerate(dados.ABAS, start=1):
            itens = self.retrato.itens[aba]
            rotulo = f" {n} {ROTULOS[aba]} "
            conta = str(len(itens))
            if aba == "crash" and itens:
                conta = f"{sum(i.estado == dados.PENDENTE for i in itens)}/{len(itens)}"
            if aba == self.aba:
                trecho = Text.assemble((rotulo, f"bold {FUNDO} on {AMBAR}"), (f" {conta} ", f"bold {AMBAR} on {PAINEL}"))
            else:
                trecho = Text.assemble((rotulo, TEXTO), (f"{conta} ", APAGADO))
            trecho.apply_meta({"@click": f"app.aba('{aba}')"})
            barra.append_text(trecho)
            barra.append("  ")
        return barra

    def _titulo_da_lista(self) -> str:
        titulo = ROTULOS[self.aba]
        if self.aba == "fechadas":
            titulo += f" nas últimas {_janela(dados.JANELAS_FECHADAS[self.janela])}"
        if self.consulta:
            titulo += f" — {len(self.visiveis)} de {len(self.retrato.itens[self.aba])}"
        return titulo

    def _celulas(self, item: dados.Item) -> list[Text]:
        apagar = item.estado == dados.REABERTA and self.aba == "crash"  # já resolvida: sai do caminho
        estado = self._estado(item)
        simbolo, cor = ESTADOS.get(estado, ("·", APAGADO))

        selo = Text.assemble((f"{item.provedor:^8}", SELOS.get(item.provedor, f"bold {FUNDO} on {APAGADO}")),
                             "\n", (f"{simbolo} {estado}", cor))
        local = ("⑂ " if item.em_worktree else "") + item.diretorio
        sub = Text(local, style=LILAS if item.em_worktree else APAGADO)
        if item.branch:
            sub.append(f"   {item.branch}", APAGADO)
        sub.truncate(self.largura_titulo, overflow="ellipsis")
        titulo = Text(item.titulo or "(sem título)", style=f"bold {TEXTO}")
        titulo.truncate(self.largura_titulo, overflow="ellipsis")
        quando = Text.assemble((f"{dados.ha_quanto(item.quando, int(self.agora())):>10}", TEXTO), "\n",
                               (f"{_hora(item.quando):>10}", APAGADO))
        celulas = [self._marca(item), selo, Text("\n").join([titulo, sub]), quando]
        if apagar:
            for celula in celulas[1:]:
                celula.stylize("dim")
        return celulas

    def _estado(self, item: dados.Item) -> str:
        return "abrindo…" if item.chave in self.reabertas_agora and not item.viva else item.estado

    def _marca(self, item: dados.Item) -> Text:
        return Text("▌\n▌", style=f"bold {AMBAR}") if item.chave in self.marcadas else Text(" \n ")

    def _atualizar_marcadas(self) -> None:
        n = len(self.marcadas)
        self.query_one("#lista").border_subtitle = (
            f"{n} marcada{'s' if n != 1 else ''} — enter reabre" if n else "")

    def _atualizar_detalhe(self) -> None:
        item = self.item_atual()
        self.query_one("#comando").display = bool(item)
        detalhe = self.query_one("#detalhe", Static)
        if not item:
            detalhe.update(Text("Escolha uma sessão na lista.", style=APAGADO))
            return
        estado = self._estado(item)
        simbolo, cor = ESTADOS.get(estado, ("·", APAGADO))
        texto = Text.assemble(
            (item.titulo or "(sem título)", f"bold {AMBAR}"), "\n\n",
            (f" {item.provedor} ", SELOS.get(item.provedor, "")), "  ", (f"{simbolo} {estado}", cor),
            (f"  {dados.ha_quanto(item.quando, int(self.agora()))}", APAGADO), "\n\n",
        )
        campos = [("retomar em", _til(item.dir_resume), TEXTO)]
        if item.em_worktree:
            campos.append(("worktree", "⑂ " + _relativo(item.dir_trabalho or item.dir_resume,
                                                         item.dir_resume), LILAS))
        else:
            campos.append(("worktree", "não — raiz do projeto", APAGADO))
        if item.branch:
            campos.append(("branch", item.branch, TEXTO))
        if item.tty:
            campos.append(("terminal", item.tty, TEXTO))
        for nome, valor, estilo in campos:
            texto.append(f"{nome}\n", APAGADO)
            texto.append(f"{valor}\n\n", estilo)
        if item.ultima_msg:
            texto.append("última mensagem\n", APAGADO)
            texto.append(item.ultima_msg[:400].strip(), f"italic {TEXTO}")
        detalhe.update(texto)
        self.query_one("#comando", Static).update(Text(self._comando(item)))

    def _comando(self, item: dados.Item) -> str:
        comando = provedores.por_nome(item.provedor).comando_resume(item.session_id)
        return f"cd {shlex.quote(item.dir_resume)} && {shlex.join(comando)}"

    def item_atual(self) -> dados.Item | None:
        linha = self.query_one(DataTable).cursor_row
        return self.visiveis[linha] if 0 <= linha < len(self.visiveis) else None

    # --- eventos ------------------------------------------------------------------

    @on(DataTable.RowHighlighted)
    def _mudou_de_linha(self) -> None:
        self._atualizar_detalhe()

    @on(DataTable.RowSelected)
    def _enter_na_tabela(self) -> None:
        self.action_reabrir()

    @on(Input.Changed, "#busca")
    def _digitou_busca(self, evento: Input.Changed) -> None:
        self.consulta = evento.value
        self.atualizar()

    @on(Input.Submitted, "#busca")
    def _confirmou_busca(self) -> None:
        self.query_one(DataTable).focus()

    # --- ações --------------------------------------------------------------------

    def action_aba(self, aba: str) -> None:
        self.aba = aba
        self.marcadas.clear()  # marca invisível em outra aba viraria aba aberta de surpresa
        self.atualizar()
        self.query_one(DataTable).move_cursor(row=0)

    def action_aba_vizinha(self, passo: int) -> None:
        if len(self.screen_stack) > 1:  # binding com prioridade: não mexer por baixo do modal
            return
        self.action_aba(dados.ABAS[(dados.ABAS.index(self.aba) + passo) % len(dados.ABAS)])

    def action_buscar(self) -> None:
        self.query_one("#busca", Input).focus()

    def action_limpar_busca(self) -> None:
        self.query_one("#busca", Input).value = ""
        self.query_one(DataTable).focus()

    def action_marcar(self) -> None:
        item = self.item_atual()
        if not item:
            return
        self.marcadas ^= {item.chave}
        tabela = self.query_one(DataTable)
        tabela.update_cell(item.chave, "marca", self._marca(item))
        tabela.action_cursor_down()
        self._atualizar_marcadas()

    def action_marcar_todas(self) -> None:
        visiveis = {i.chave for i in self.visiveis}
        if visiveis <= self.marcadas:
            self.marcadas -= visiveis
        else:
            self.marcadas |= visiveis
        self.atualizar()

    def action_janela(self) -> None:
        if self.aba != "fechadas":
            self.notify("A janela de tempo só vale para a aba Fechadas.")
            return
        self.janela = (self.janela + 1) % len(dados.JANELAS_FECHADAS)
        self.action_recarregar()

    def action_detalhe(self) -> None:
        self.query_one("#painel").toggle_class("oculto")
        self.set_timer(0.1, self._ajustar_colunas)  # depois do layout

    def action_recarregar(self) -> None:
        self.carregar()
        self.atualizar()

    def action_copiar(self) -> None:
        item = self.item_atual()
        if item:
            self.copy_to_clipboard(self._comando(item))
            self.notify("Comando copiado.")

    def action_reabrir(self) -> None:
        if self.marcadas:
            alvos = [i for i in self.retrato.itens[self.aba] if i.chave in self.marcadas]
        else:
            alvos = [i for i in (self.item_atual(),) if i]
        ja_abertas = [i for i in alvos if i.viva or i.chave in self.reabertas_agora]
        alvos = [i for i in alvos if i not in ja_abertas]
        if ja_abertas:
            self.notify("Já está aberta — nada a fazer." if len(ja_abertas) == 1
                        else f"{len(ja_abertas)} já estão abertas — ignoradas.", severity="warning")
        if not alvos:
            return
        if len(alvos) > CONFIRMAR_ACIMA_DE:
            self.push_screen(Confirmar(f"Abrir {len(alvos)} abas no Windows Terminal?"),
                             lambda sim: self._abrir(alvos) if sim else None)
        else:
            self._abrir(alvos)

    @work(thread=True, exclusive=True, group="reabrir")
    def _abrir(self, alvos: list[dados.Item]) -> None:
        # Em thread: o wt.exe demora e há uma pausa entre uma aba e outra.
        try:
            r = self.reabrir_sessoes([i.como_sessao() for i in alvos])
        except reabrir.ErroReabrir as e:
            self.call_from_thread(self.notify, str(e), severity="error", timeout=10)
            return
        self.call_from_thread(self._abriu, r)

    def _abriu(self, r: reabrir.Resultado) -> None:
        self.reabertas_agora |= {f"{s['provedor']}:{s['session_id']}" for s in r.abertas}
        self.marcadas.clear()
        if r.abertas:
            n = len(r.abertas)
            self.notify(f"{n} aba{'s' if n != 1 else ''} aberta{'s' if n != 1 else ''} no Windows Terminal.")
        for s, motivo in r.puladas:
            self.notify(f"{s['titulo'] or s['session_id'][:8]}: {motivo}", severity="warning", timeout=10)
        self.atualizar()


def _hora(epoch: float) -> str:
    quando = datetime.fromtimestamp(epoch)
    formato = "%H:%M" if quando.date() == datetime.now().date() else "%d/%m %H:%M"
    return quando.strftime(formato)


def _janela(horas: int) -> str:
    return f"{horas}h" if horas < 72 else f"{horas // 24} dias"


def _relativo(caminho: str, base: str) -> str:
    """Worktree dentro do projeto aparece relativo a ele — o caminho inteiro não cabe no painel."""
    if caminho != base and caminho.startswith(base.rstrip("/") + "/"):
        return caminho[len(base.rstrip("/")) + 1:]
    return _til(caminho)


def _til(caminho: str) -> str:
    casa = str(Path.home())
    return "~" + caminho[len(casa):] if caminho.startswith(casa) else caminho
