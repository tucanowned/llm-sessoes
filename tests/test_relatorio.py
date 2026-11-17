import json
import os
import socket
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from llm_sessoes import __version__, relatorio, sistema
from llm_sessoes.banco import Banco
from llm_sessoes.coletor import coletar
from llm_sessoes.modelos import SessaoViva


class ProvedorFalso:
    nome = "claude"

    def __init__(self, sessoes):
        self.sessoes = sessoes
        self.avisos = []

    def sessoes_vivas(self):
        return list(self.sessoes)


class TmuxTest(unittest.TestCase):
    """`tmux_do_processo` com /proc simulado e um executor falso no lugar do tmux."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.proc = Path(self.tmp.name)
        self.chamadas = []

    def tearDown(self):
        self.tmp.cleanup()

    def ambiente(self, pid, **variaveis):
        d = self.proc / str(pid)
        d.mkdir()
        (d / "environ").write_bytes(
            b"".join(f"{k}={v}".encode() + b"\0" for k, v in variaveis.items()))

    def executor(self, saida):
        def executar(argv):
            self.chamadas.append(argv)
            return saida
        return executar

    def test_resolve_sessao_e_janela_pelo_socket_e_painel_do_processo(self):
        self.ambiente(10, HOME="/home/x", TMUX="/tmp/tmux-1000/default,999,0", TMUX_PANE="%7")

        tmux = sistema.tmux_do_processo(10, self.proc, self.executor("trabalho:3\n"))

        self.assertEqual({"sessao": "trabalho", "janela": 3, "painel": "%7"}, tmux)
        self.assertEqual([["tmux", "-S", "/tmp/tmux-1000/default", "display", "-p", "-t", "%7",
                           "#S:#I"]], self.chamadas)

    def test_processo_fora_do_tmux_nem_chama_o_tmux(self):
        self.ambiente(10, HOME="/home/x")

        self.assertIsNone(sistema.tmux_do_processo(10, self.proc, self.executor("x:1")))
        self.assertEqual([], self.chamadas)

    def test_painel_que_nao_existe_mais(self):
        self.ambiente(10, TMUX="/tmp/s,1,0", TMUX_PANE="%7")

        self.assertIsNone(sistema.tmux_do_processo(10, self.proc, self.executor(None)))

    def test_processo_sumiu(self):
        self.assertIsNone(sistema.tmux_do_processo(42, self.proc, self.executor("x:1")))

    def test_ambiente_com_valor_contendo_igual(self):
        self.ambiente(10, OPCOES="a=b=c")

        self.assertEqual({"OPCOES": "a=b=c"}, sistema.ler_ambiente(10, self.proc))


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.arquivo = Path(self.tmp.name) / "central.env"

    def tearDown(self):
        self.tmp.cleanup()

    def test_sem_url_nao_ha_config(self):
        self.assertIsNone(relatorio.carregar_config({"LLM_SESSOES_TOKEN": "t"}, self.arquivo))

    def test_arquivo_como_fallback_e_ambiente_com_precedencia(self):
        self.arquivo.write_text("# painel\n"
                                "export LLM_SESSOES_CENTRAL_URL='https://central.exemplo/'\n"
                                'LLM_SESSOES_TOKEN="do-arquivo"\n')

        config = relatorio.carregar_config({"LLM_SESSOES_TOKEN": "do-ambiente",
                                            "LLM_SESSOES_MAQUINA": "pc"}, self.arquivo)

        self.assertEqual(relatorio.Config("https://central.exemplo", "do-ambiente", "pc"), config)

    def test_maquina_padrao_e_o_hostname(self):
        config = relatorio.carregar_config({"LLM_SESSOES_CENTRAL_URL": "http://x"}, self.arquivo)

        self.assertEqual(socket.gethostname(), config.maquina)


class MontarTest(unittest.TestCase):
    def setUp(self):
        self.banco = Banco(":memory:")

    def tearDown(self):
        self.banco.fechar()

    def test_payload_com_vivas_tmux_eventos_e_anomalias(self):
        a = SessaoViva("claude", "a", 10, dir_resume="/repo", dir_trabalho="/repo/.wt/x",
                       status="busy", titulo="Sessão A", branch="feat/x",
                       ultima_msg="m" * 500, ultima_atividade=1500)
        b = SessaoViva("claude", "b", 20, dir_resume="/outro", status="idle")
        tmux = {10: {"sessao": "dev", "janela": 2, "painel": "%3"}}
        coletar(self.banco, [ProvedorFalso([a, b])], 1000, "boot-1", tmux_de=tmux.get)
        coletar(self.banco, [ProvedorFalso([a])], 2000, "boot-1", tmux_de=tmux.get)

        p = relatorio.montar(self.banco, "pc", "boot-1", 2000)

        self.assertEqual((1, "pc", "boot-1", 2000, __version__),
                         (p["versao_esquema"], p["maquina"], p["boot_id"], p["coletado_em"],
                          p["versao_cliente"]))
        [viva] = p["vivas"]
        self.assertEqual({
            "provedor": "claude", "id": "a", "titulo": "Sessão A", "ultima_msg": "m" * 300,
            "dir_resume": "/repo", "dir_trabalho": "/repo/.wt/x", "branch": "feat/x",
            "status": "trabalhando", "inicio": 1000, "ultima_atividade": 1500,
            "tmux": {"sessao": "dev", "janela": 2, "painel": "%3"},
        }, viva)
        self.assertEqual([("abriu", "a", 1000), ("abriu", "b", 1000), ("fechou", "b", 2000)],
                         [(e["tipo"], e["id"], e["quando"]) for e in p["eventos"]])
        fechou = p["eventos"][-1]
        self.assertEqual((None, None, "/outro"), (fechou["status"], fechou["tmux"],
                                                  fechou["dir_resume"]))
        json.dumps(p)  # serializável

    def test_eventos_so_das_ultimas_48h_e_anomalias(self):
        coletar(self.banco, [ProvedorFalso([SessaoViva("claude", "a", 10, "/r")])], 1000, "b")
        self.banco.definir_meta("avisos", json.dumps(["claude: algo estranho"]))
        agora = 1000 + 48 * 3600 + 1

        p = relatorio.montar(self.banco, "pc", "b", agora)

        self.assertEqual([], p["eventos"])
        self.assertEqual(["claude: algo estranho"], p["anomalias"])
        self.assertEqual("parada", relatorio.STATUS["idle"])

    def test_banco_antigo_ganha_as_colunas_novas(self):
        with tempfile.TemporaryDirectory() as tmp:
            caminho = Path(tmp) / "velho.db"
            con = sqlite3.connect(caminho)
            con.executescript("""
                CREATE TABLE sessoes (provedor TEXT, session_id TEXT, titulo TEXT,
                    dir_resume TEXT NOT NULL, dir_trabalho TEXT, branch TEXT, ultima_msg TEXT,
                    primeira_vez INTEGER NOT NULL, ultima_vez INTEGER NOT NULL,
                    PRIMARY KEY (provedor, session_id));
                CREATE TABLE vivas (provedor TEXT, session_id TEXT, pid INTEGER NOT NULL,
                    tty TEXT, status TEXT, desde INTEGER NOT NULL,
                    PRIMARY KEY (provedor, session_id));
            """)
            con.close()

            banco = Banco(caminho)
            coletar(banco, [ProvedorFalso([SessaoViva("claude", "a", 10, "/r")])], 1000, "b",
                    tmux_de=lambda pid: {"sessao": "s", "janela": 1, "painel": "%1"})

            self.assertEqual("s", relatorio.montar(banco, "pc", "b", 1000)["vivas"][0]["tmux"]["sessao"])
            banco.fechar()


class EnviarTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"HOME": self.tmp.name,
                                                "XDG_CONFIG_HOME": self.tmp.name})
        self.env.start()
        for chave in ("LLM_SESSOES_CENTRAL_URL", "LLM_SESSOES_TOKEN", "LLM_SESSOES_MAQUINA"):
            os.environ.pop(chave, None)
        self.banco = Banco(":memory:")

    def tearDown(self):
        self.banco.fechar()
        self.env.stop()
        self.tmp.cleanup()

    def test_sem_configuracao_nao_faz_nada(self):
        with mock.patch("urllib.request.urlopen") as urlopen:
            self.assertIsNone(relatorio.enviar_do_banco(self.banco, "b", 1000))
        urlopen.assert_not_called()
        self.assertFalse(relatorio.caminho_log().exists())

    def test_rede_fora_vira_linha_no_log_e_nao_levanta(self):
        porta_fechada = _porta_livre()
        config = relatorio.Config(f"http://127.0.0.1:{porta_fechada}", "t", "pc")

        self.assertFalse(relatorio.enviar_do_banco(self.banco, "b", 1000, config))

        log = relatorio.caminho_log().read_text()
        self.assertIn("enviar ao painel central falhou: sem conexão", log)
        self.assertEqual(1, len(log.splitlines()))

    def test_erro_inesperado_tambem_nao_levanta(self):
        config = relatorio.Config("http://x", "t", "pc")
        with mock.patch.object(relatorio, "montar", side_effect=RuntimeError("bug")):
            self.assertFalse(relatorio.enviar_do_banco(self.banco, "b", 1000, config))
        self.assertIn("RuntimeError", relatorio.caminho_log().read_text())


def _porta_livre() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


if __name__ == "__main__":
    unittest.main()
