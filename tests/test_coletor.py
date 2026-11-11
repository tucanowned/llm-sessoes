import unittest

from llm_sessoes.banco import Banco
from llm_sessoes.coletor import coletar
from llm_sessoes.modelos import SessaoViva


class ProvedorFalso:
    def __init__(self, nome="claude"):
        self.nome = nome
        self.sessoes = []
        self.erro = None
        self.avisos = []

    def sessoes_vivas(self):
        if self.erro:
            raise self.erro
        return list(self.sessoes)


def sessao(session_id, pid=100, provedor="claude", **extra):
    return SessaoViva(provedor=provedor, session_id=session_id, pid=pid,
                      dir_resume="/repo", **extra)


class ColetorTest(unittest.TestCase):
    def setUp(self):
        self.banco = Banco(":memory:")
        self.provedor = ProvedorFalso()

    def tearDown(self):
        self.banco.fechar()

    def coletar(self, agora, boot="boot-1", provedores=None):
        return coletar(self.banco, provedores or [self.provedor], agora, boot)

    def eventos(self):
        linhas = self.banco.con.execute(
            "SELECT tipo, session_id, quando FROM eventos ORDER BY id").fetchall()
        return [tuple(l) for l in linhas]

    def test_sessao_nova_gera_evento_abriu(self):
        self.provedor.sessoes = [sessao("a", titulo="Minha sessão")]

        r = self.coletar(1000)

        self.assertEqual([("claude", "a")], r.abriram)
        self.assertEqual([("abriu", "a", 1000)], self.eventos())
        self.assertEqual("Minha sessão", self.banco.vivas()[0]["titulo"])

    def test_sessao_que_continua_viva_nao_gera_evento(self):
        self.provedor.sessoes = [sessao("a")]
        self.coletar(1000)

        r = self.coletar(1120)

        self.assertEqual([], r.abriram + r.fecharam)
        self.assertEqual(1, len(self.eventos()))

    def test_sessao_que_sumiu_gera_evento_fechou(self):
        self.provedor.sessoes = [sessao("a"), sessao("b", pid=200)]
        self.coletar(1000)
        self.provedor.sessoes = [sessao("b", pid=200)]

        r = self.coletar(1120)

        self.assertEqual([("claude", "a")], r.fecharam)
        self.assertEqual(["b"], [v["session_id"] for v in self.banco.vivas()])
        self.assertEqual(["a"], [f["session_id"] for f in self.banco.fechadas(desde=0)])

    def test_fechada_e_reaberta_sai_da_lista_de_fechadas(self):
        self.provedor.sessoes = [sessao("a")]
        self.coletar(1000)
        self.provedor.sessoes = []
        self.coletar(1120)
        self.provedor.sessoes = [sessao("a", pid=300)]

        self.coletar(1240)

        self.assertEqual([], self.banco.fechadas(desde=0))
        self.assertEqual(300, self.banco.vivas()[0]["pid"])

    def test_troca_de_boot_vira_crash_datado_da_ultima_coleta(self):
        self.provedor.sessoes = [sessao("a"), sessao("b", pid=200)]
        self.coletar(1000, boot="boot-1")
        self.provedor.sessoes = []

        r = self.coletar(5000, boot="boot-2")

        self.assertCountEqual([("claude", "a"), ("claude", "b")], r.crash)
        self.assertEqual([], r.fecharam)
        crash = self.banco.ultimo_crash()
        self.assertCountEqual(["a", "b"], [c["session_id"] for c in crash])
        self.assertEqual({1000}, {c["quando"] for c in crash})

    def test_sessao_reaberta_apos_crash_fica_marcada(self):
        self.provedor.sessoes = [sessao("a"), sessao("b", pid=200)]
        self.coletar(1000, boot="boot-1")
        self.provedor.sessoes = [sessao("a", pid=50)]

        self.coletar(5000, boot="boot-2")

        reaberta = {c["session_id"]: c["reaberta"] for c in self.banco.ultimo_crash()}
        self.assertEqual({"a": 1, "b": 0}, reaberta)

    def test_ultimo_crash_ignora_crashes_antigos(self):
        self.provedor.sessoes = [sessao("velha")]
        self.coletar(1000, boot="boot-1")
        self.provedor.sessoes = [sessao("nova")]
        self.coletar(2000, boot="boot-2")
        self.coletar(3000, boot="boot-3")

        self.assertEqual(["nova"], [c["session_id"] for c in self.banco.ultimo_crash()])

    def test_provedor_com_erro_nao_fecha_suas_sessoes_nem_afeta_os_outros(self):
        codex = ProvedorFalso("codex")
        self.provedor.sessoes = [sessao("a")]
        codex.sessoes = [sessao("x", provedor="codex")]
        self.coletar(1000, provedores=[self.provedor, codex])
        self.provedor.erro = RuntimeError("formato mudou")
        codex.sessoes = []

        r = self.coletar(1120, provedores=[self.provedor, codex])

        self.assertEqual([("codex", "x")], r.fecharam)
        self.assertEqual(["a"], [v["session_id"] for v in self.banco.vivas()])
        self.assertIn("claude", r.avisos[0])

    def test_metadado_ausente_nao_apaga_o_ja_conhecido(self):
        self.provedor.sessoes = [sessao("a", titulo="Título", branch="main")]
        self.coletar(1000)
        self.provedor.sessoes = [sessao("a", titulo=None, branch="outra")]

        self.coletar(1120)

        viva = self.banco.vivas()[0]
        self.assertEqual(("Título", "outra"), (viva["titulo"], viva["branch"]))

    def test_mesmo_id_em_provedores_diferentes_nao_colide(self):
        codex = ProvedorFalso("codex")
        self.provedor.sessoes = [sessao("a")]
        codex.sessoes = [sessao("a", provedor="codex")]

        r = self.coletar(1000, provedores=[self.provedor, codex])

        self.assertEqual(2, r.vivas)
        self.assertEqual(2, len(self.banco.vivas()))

    def test_provedor_com_anomalia_registra_aberturas_mas_nao_fechamentos(self):
        self.provedor.sessoes = [sessao("a")]
        self.coletar(1000)
        self.provedor.sessoes = [sessao("nova", pid=200)]
        self.provedor.avisos = ["processo pid=100 sem registro — formato interno mudou?"]

        r = self.coletar(1120)

        self.assertEqual(([("claude", "nova")], []), (r.abriram, r.fecharam))
        self.assertCountEqual(["a", "nova"], [v["session_id"] for v in self.banco.vivas()])
        self.assertIn("formato interno mudou", r.avisos[0])

    def test_avisos_ficam_gravados_ate_a_proxima_coleta_limpa(self):
        self.provedor.avisos = ["algo estranho"]
        self.coletar(1000)
        self.assertIn("algo estranho", self.banco.meta("avisos"))

        self.provedor.avisos = []
        self.coletar(1120)

        self.assertEqual("[]", self.banco.meta("avisos"))

    def test_anomalia_resolvida_volta_a_registrar_fechamento(self):
        self.provedor.sessoes = [sessao("a")]
        self.coletar(1000)
        self.provedor.sessoes, self.provedor.avisos = [], ["algo estranho"]
        self.coletar(1120)
        self.provedor.avisos = []

        r = self.coletar(1240)

        self.assertEqual([("claude", "a")], r.fecharam)

    def test_versao_nova_vira_nota_uma_unica_vez(self):
        self.provedor.sessoes = [sessao("a", versao="2.1.0")]
        self.assertEqual([], self.coletar(1000).notas)  # primeira coleta não conta

        self.provedor.sessoes = [sessao("a", versao="2.1.0"), sessao("b", pid=2, versao="2.2.0")]
        self.assertEqual(["claude: versão nova em uso: 2.2.0"], self.coletar(1120).notas)
        self.assertEqual([], self.coletar(1240).notas)


if __name__ == "__main__":
    unittest.main()
