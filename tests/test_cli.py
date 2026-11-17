import contextlib
import io
import os
import socket
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from llm_sessoes import cli, reabrir, relatorio
from llm_sessoes.banco import Banco
from llm_sessoes.coletor import coletar
from llm_sessoes.modelos import SessaoViva


class ProvedorFalso:
    nome = "claude"

    def __init__(self, sessoes):
        self.sessoes = sessoes

    def sessoes_vivas(self):
        return self.sessoes


def sessao(session_id, titulo, pid, dir_resume="/repo com espaço"):
    return SessaoViva("claude", session_id, pid, dir_resume=dir_resume,
                      dir_trabalho="/repo com espaço/.claude/worktrees/x",
                      titulo=titulo, branch="feat/x", tty="pts/1")


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.caminho = Path(self.tmp.name) / "sub" / "sessoes.db"
        # HOME/XDG_CONFIG_HOME no temporário: nenhum teste pode achar o central.env de verdade.
        self.env = mock.patch.dict(os.environ, {"LLM_SESSOES_DB": str(self.caminho),
                                                "HOME": self.tmp.name,
                                                "XDG_CONFIG_HOME": self.tmp.name})
        self.env.start()
        for chave in ("LLM_SESSOES_CENTRAL_URL", "LLM_SESSOES_TOKEN", "LLM_SESSOES_MAQUINA"):
            os.environ.pop(chave, None)
        self.agora = int(time.time())

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def coletar(self, sessoes, quando, boot="boot-1"):
        banco = Banco(self.caminho)
        coletar(banco, [ProvedorFalso(sessoes)], quando, boot)
        banco.fechar()

    def rodar(self, *argv):
        saida = io.StringIO()
        with contextlib.redirect_stdout(saida):
            codigo = cli.main(list(argv))
        return codigo, saida.getvalue()

    def test_status_sem_coleta(self):
        codigo, saida = self.rodar("status")

        self.assertEqual(1, codigo)
        self.assertIn("Nenhuma coleta", saida)

    def test_status_mostra_comando_de_resume_com_diretorio_escapado(self):
        self.coletar([sessao("id-1", "Perf dashboard", 10)], self.agora)

        _, saida = self.rodar("status")

        self.assertIn("Perf dashboard", saida)
        self.assertIn("worktrees/x", saida)
        self.assertIn("cd '/repo com espaço' && claude --resume id-1", saida)

    def test_fechadas_respeita_a_janela_de_horas(self):
        a, b = sessao("id-a", "Antiga", 10), sessao("id-b", "Recente", 20)
        self.coletar([a, b], self.agora - 10 * 3600)
        self.coletar([b], self.agora - 9 * 3600)
        self.coletar([], self.agora - 60)

        _, saida = self.rodar("fechadas", "--horas", "1")

        self.assertIn("Recente", saida)
        self.assertNotIn("Antiga", saida)

    def test_crash_separa_pendentes_de_reabertas(self):
        a, b = sessao("id-a", "Voltou", 10), sessao("id-b", "Ficou para trás", 20)
        self.coletar([a, b], self.agora - 600, boot="boot-1")
        self.coletar([a], self.agora, boot="boot-2")

        _, saida = self.rodar("crash")

        self.assertIn("2 sessão(ões)", saida)
        self.assertIn("1 ainda não reaberta", saida)
        self.assertRegex(saida, r"Ficou para trás\s+—\s+PENDENTE")
        self.assertRegex(saida, r"Voltou\s+—\s+já reaberta")

    # --- reabrir ------------------------------------------------------------------

    def reabrir(self, *argv):
        """Roda `reabrir` com o wt trocado por um gravador; devolve (código, saída, ids abertos)."""
        chamadas = []
        with mock.patch.object(reabrir, "localizar_wt", return_value="wt.exe"), \
                mock.patch.object(reabrir, "_executar", chamadas.append), \
                mock.patch.object(reabrir, "PAUSA_ENTRE_ABAS", 0), \
                mock.patch.object(reabrir.time, "sleep"):
            codigo, saida = self.rodar("reabrir", *argv)
        return codigo, saida, [c[-1] for c in chamadas]

    def test_reabrir_crash_abre_so_as_pendentes(self):
        a, b = (sessao("id-a", "Voltou", 10, self.tmp.name),
                sessao("id-b", "Ficou para trás", 20, self.tmp.name))
        self.coletar([a, b], self.agora - 600, boot="boot-1")
        self.coletar([a], self.agora, boot="boot-2")

        codigo, saida, abertos = self.reabrir("--crash")

        self.assertEqual(0, codigo)
        self.assertEqual(["id-b"], abertos)
        self.assertIn("Ficou para trás", saida)

    def test_reabrir_fechadas_respeita_a_janela(self):
        a, b = (sessao("id-a", "Antiga", 10, self.tmp.name),
                sessao("id-b", "Recente", 20, self.tmp.name))
        self.coletar([a, b], self.agora - 10 * 3600)
        self.coletar([b], self.agora - 9 * 3600)
        self.coletar([], self.agora - 60)

        _, _, abertos = self.reabrir("--fechadas", "--horas", "1")

        self.assertEqual(["id-b"], abertos)

    def test_reabrir_por_prefixo(self):
        self.coletar([sessao("abc-1", "Alvo", 10, self.tmp.name),
                      sessao("xyz-2", "Outra", 20, self.tmp.name)], self.agora - 600)
        self.coletar([], self.agora)

        codigo, _, abertos = self.reabrir("abc")

        self.assertEqual(0, codigo)
        self.assertEqual(["abc-1"], abertos)

    def test_reabrir_prefixo_ambiguo_nao_abre_nada(self):
        self.coletar([sessao("abc-1", "Uma", 10, self.tmp.name),
                      sessao("abc-2", "Outra", 20, self.tmp.name)], self.agora - 600)
        self.coletar([], self.agora)

        erro = io.StringIO()
        with contextlib.redirect_stderr(erro):
            codigo, _, abertos = self.reabrir("abc")

        self.assertEqual(2, codigo)
        self.assertEqual([], abertos)
        self.assertIn("ambíguo", erro.getvalue())
        self.assertIn("abc-2", erro.getvalue())

    def test_reabrir_prefixo_de_sessao_viva_e_pulado(self):
        self.coletar([sessao("abc-1", "Aberta", 10, self.tmp.name)], self.agora)

        _, saida, abertos = self.reabrir("abc")

        self.assertEqual([], abertos)
        self.assertIn("Já está aberta", saida)

    def test_reabrir_simular_nao_abre(self):
        self.coletar([sessao("abc-1", "Alvo", 10, self.tmp.name)], self.agora - 600)
        self.coletar([], self.agora)

        _, saida, abertos = self.reabrir("abc", "-n")

        self.assertEqual([], abertos)
        self.assertIn("abriria", saida)

    def test_reabrir_avisa_diretorio_que_sumiu(self):
        self.coletar([sessao("abc-1", "Órfã", 10, self.tmp.name + "/sumiu")], self.agora - 600)
        self.coletar([], self.agora)

        codigo, saida, abertos = self.reabrir("abc")

        self.assertEqual(1, codigo)
        self.assertEqual([], abertos)
        self.assertIn("PULADA", saida)

    def test_aba_roda_o_resume_da_sessao_do_banco(self):
        self.coletar([sessao("abc-1", "Alvo", 10, self.tmp.name)], self.agora)

        with mock.patch.object(reabrir, "rodar_na_aba") as rodar_na_aba:
            self.rodar("_aba", "claude", "abc-1")

        self.assertEqual(self.tmp.name, rodar_na_aba.call_args.args[0]["dir_resume"])

    # --- aviso e hook -------------------------------------------------------------

    def aviso(self, boot):
        with mock.patch.object(cli.sistema, "boot_id", return_value=boot), \
                mock.patch.object(cli.provedores, "todos", return_value=[ProvedorFalso([])]):
            return self.rodar("aviso")[1]

    def test_aviso_detecta_o_crash_antes_do_cron_e_so_fala_uma_vez_por_boot(self):
        self.coletar([sessao("id-a", "Ficou para trás", 10)], self.agora - 600, boot="boot-1")

        primeira = self.aviso("boot-2")
        segunda = self.aviso("boot-2")

        self.assertIn("1 sessão estava aberta", primeira)
        self.assertIn("Ficou para trás", primeira)
        self.assertIn("reabrir --crash", primeira)
        self.assertEqual("", segunda)

    def test_aviso_calado_sem_crash_pendente(self):
        self.coletar([sessao("id-a", "Normal", 10)], self.agora, boot="boot-1")

        self.assertEqual("", self.aviso("boot-1"))

    def test_aviso_esquece_crash_antigo(self):
        velho = self.agora - 30 * 86400
        self.coletar([sessao("id-a", "Antiga", 10)], velho, boot="boot-1")

        self.assertEqual("", self.aviso("boot-2"))

    def test_aviso_nunca_quebra_o_shell(self):
        with mock.patch.object(cli, "_aviso", side_effect=RuntimeError("banco corrompido")):
            codigo, saida = self.rodar("aviso")

        self.assertEqual((0, ""), (codigo, saida))

    def hook(self, evento):
        with mock.patch.object(cli.sys, "stdin", io.StringIO(evento)), \
                mock.patch.object(cli.sistema, "boot_id", return_value="boot-1"):
            return self.rodar("hook", "claude")[0]

    def test_hook_session_end_registra_o_fechamento_na_hora(self):
        self.coletar([sessao("id-a", "Fechei agora", 10, self.tmp.name)], self.agora - 60)

        codigo = self.hook('{"hook_event_name": "SessionEnd", "session_id": "id-a", "reason": "exit"}')

        self.assertEqual(0, codigo)
        self.assertIn("Fechei agora", self.rodar("fechadas")[1])
        self.assertIn("0 sessão(ões) viva(s)", self.rodar("status")[1])

    def test_coleta_seguinte_nao_duplica_o_fechamento_do_hook(self):
        self.coletar([sessao("id-a", "Fechei agora", 10)], self.agora - 60)
        self.hook('{"hook_event_name": "SessionEnd", "session_id": "id-a"}')
        self.coletar([], self.agora)

        banco = Banco(self.caminho)
        fechou = banco.con.execute("SELECT COUNT(*) FROM eventos WHERE tipo = 'fechou'").fetchone()[0]
        banco.fechar()

        self.assertEqual(1, fechou)

    def test_hook_ignora_lixo_e_sessao_desconhecida(self):
        self.assertEqual(0, self.hook("isto não é json"))
        self.assertEqual(0, self.hook('{"hook_event_name": "SessionEnd", "session_id": "nunca-vi"}'))
        self.assertEqual(0, self.hook('{"hook_event_name": "Stop", "session_id": "x"}'))

    # --- painel central -------------------------------------------------------------

    def test_enviar_sem_configuracao_nao_faz_nada(self):
        with mock.patch.object(relatorio, "enviar") as enviar:
            self.assertEqual((0, ""), self.rodar("enviar"))
        enviar.assert_not_called()

    def test_enviar_com_rede_fora_sai_com_erro_e_anota_no_log(self):
        os.environ["LLM_SESSOES_CENTRAL_URL"] = f"http://127.0.0.1:{_porta_livre()}"
        erro = io.StringIO()
        with contextlib.redirect_stderr(erro):
            codigo, _ = self.rodar("enviar")

        self.assertEqual(1, codigo)
        self.assertIn("ERRO ao enviar", erro.getvalue())
        self.assertIn("falhou", relatorio.caminho_log().read_text())

    def test_coletar_envia_no_fim_e_falha_de_rede_nao_muda_o_resultado(self):
        os.environ["LLM_SESSOES_CENTRAL_URL"] = f"http://127.0.0.1:{_porta_livre()}"
        with mock.patch.object(cli.provedores, "todos", return_value=[ProvedorFalso([])]), \
                mock.patch.object(cli.sistema, "boot_id", return_value="boot-1"):
            codigo, _ = self.rodar("coletar")

        self.assertEqual(0, codigo)
        self.assertIn("enviar ao painel central falhou", relatorio.caminho_log().read_text())

    def test_hook_envia_o_retrato_depois_do_fechamento(self):
        self.coletar([sessao("id-a", "Fechei agora", 10)], self.agora - 60)
        os.environ["LLM_SESSOES_CENTRAL_URL"] = "http://central"
        with mock.patch.object(relatorio, "enviar") as enviar:
            self.hook('{"hook_event_name": "SessionEnd", "session_id": "id-a"}')

        [(config, payload), _] = enviar.call_args
        self.assertEqual("http://central", config.url)
        self.assertEqual([], payload["vivas"])
        self.assertEqual("fechou", payload["eventos"][-1]["tipo"])


def _porta_livre() -> int:
    """Porta sem ninguém escutando: a conexão é recusada na hora."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


if __name__ == "__main__":
    unittest.main()
