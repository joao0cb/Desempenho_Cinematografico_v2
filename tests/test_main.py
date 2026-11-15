"""Testes da CLI: orquestração, validação de entrada e mensagens de erro (issue #11)."""
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

import main as cli
from main import conectar, inicializar_schema, inserir_filmes
from main import Movie
from main import media, mediana, metrica

RAIZ = Path(__file__).resolve().parent.parent


def _filmes_de_teste() -> tuple[Movie, ...]:
    return tuple(
        Movie(
            id=i, titulo=f"Filme {i}", ano=1990 + (i % 3) * 10 + i % 7,
            generos=(("Terror",), ("Drama", "Ação"), ("Comédia",))[i % 3],
            orcamento=(10 + i * 3) * 1_000_000,
            faturamento=(30 + i * i) * 1_000_000,
            nota=5.0 + (i % 5) * 0.8,
        )
        for i in range(1, 25)
    ) + (Movie(id=99, titulo="Sem orçamento", ano=2000, faturamento=1_000_000, nota=7.0),)


@pytest.fixture
def banco(tmp_path) -> str:
    caminho = tmp_path / "teste.db"
    with closing(conectar(caminho)) as conn:
        inicializar_schema(conn)
        inserir_filmes(conn, _filmes_de_teste())
    return str(caminho)


class Sessao:
    """Entradas roteirizadas e saída capturada, no lugar de teclado e terminal."""

    def __init__(self, *entradas: str):
        self._entradas = iter(entradas)
        self.perguntas: list[str] = []
        self.saida: list[str] = []

    def ler(self, prompt: str) -> str:
        self.perguntas.append(prompt)
        return next(self._entradas)

    def escrever(self, texto: str) -> None:
        self.saida.append(texto)

    @property
    def texto(self) -> str:
        return "\n".join(self.saida)

    def menus_exibidos(self) -> int:
        return sum("Escolha uma opção" in p for p in self.perguntas)


def rodar(banco: str, *entradas: str) -> tuple[int, Sessao]:
    sessao = Sessao(*entradas)
    codigo = cli.main(["--banco", banco], ler=sessao.ler, escrever=sessao.escrever)
    return codigo, sessao


@pytest.fixture
def filmes(banco) -> tuple[Movie, ...]:
    return cli.carregar_filmes(banco)


# ---------------------------------------------------------------- pipeline
def test_pipeline_compoe_selecao_transformacao_e_agregacao():
    base = _filmes_de_teste()
    notas = pipeline_notas = cli.pipeline(base, lambda f: f.nota > 0, metrica("nota"), media)
    esperado = media(f.nota for f in base if f.nota > 0)
    assert notas == pytest.approx(esperado)


def test_pipeline_descarta_none_antes_de_agregar():
    base = (Movie(id=1, titulo="a", ano=2000, orcamento=10, faturamento=30),
            Movie(id=2, titulo="b", ano=2000, orcamento=0, faturamento=50))
    # roi do filme 2 é None: a mediana vê só um valor (2.0)
    assert cli.pipeline(base, lambda f: True, metrica("roi"), mediana) == pytest.approx(2.0)


def test_pipeline_e_preguicoso():
    chamadas = []
    def transformar(f):
        chamadas.append(f.id)
        return 1.0
    infinito = (Movie(id=i, titulo="x", ano=2000) for i in range(10**9))
    primeiro = cli.pipeline(infinito, lambda f: f.id < 3, transformar, lambda vs: next(iter(vs)))
    assert primeiro == 1.0 and chamadas == [0]


# ---------------------------------------------------------- carregar_filmes
def test_carregar_aplica_a_limpeza_e_devolve_tupla(banco):
    filmes = cli.carregar_filmes(banco)
    assert isinstance(filmes, tuple) and len(filmes) == 24     # o id 99 (sem orçamento) saiu


def test_banco_ausente_levanta_erro_amigavel_e_nao_cria_arquivo(tmp_path):
    caminho = tmp_path / "pasta_nova" / "nao_existe.db"
    with pytest.raises(RuntimeError, match="--ingerir"):
        cli.carregar_filmes(caminho)
    assert not caminho.exists() and not caminho.parent.exists()


def test_arquivo_que_nao_e_banco_levanta_erro_amigavel(tmp_path):
    lixo = tmp_path / "lixo.db"
    lixo.write_text("isto não é sqlite")
    with pytest.raises(RuntimeError, match="não é um banco válido"):
        cli.carregar_filmes(lixo)


def test_banco_sem_tabela_levanta_erro_amigavel(tmp_path):
    vazio = tmp_path / "sem_tabela.db"
    with closing(conectar(vazio)):
        pass
    with pytest.raises(RuntimeError, match="não é um banco válido"):
        cli.carregar_filmes(vazio)


def test_banco_sem_filmes_validos_levanta_erro_amigavel(tmp_path):
    caminho = tmp_path / "vazio.db"
    with closing(conectar(caminho)) as conn:
        inicializar_schema(conn)
    with pytest.raises(RuntimeError, match="não tem filmes"):
        cli.carregar_filmes(caminho)


# ------------------------------------------------------------------- main
def test_main_banco_ausente_devolve_1_com_mensagem_sem_traceback(tmp_path):
    sessao = Sessao()
    codigo = cli.main(["--banco", str(tmp_path / "x.db")], ler=sessao.ler, escrever=sessao.escrever)
    assert codigo == 1
    assert sessao.texto.startswith("erro: banco não encontrado") and "--ingerir" in sessao.texto
    assert sessao.perguntas == []                                # nem chegou a abrir o menu


def test_main_sair_devolve_0(banco):
    codigo, sessao = rodar(banco, "0")
    assert codigo == 0 and "Base carregada: 24 filmes" in sessao.texto
    assert "Até logo!" in sessao.texto


@pytest.mark.parametrize("saida", ["0", "sair", "SAIR", " s ", "S"])
def test_varias_formas_de_sair(banco, saida):
    assert rodar(banco, saida)[0] == 0


def test_opcao_invalida_repete_o_menu_em_vez_de_derrubar(banco):
    codigo, sessao = rodar(banco, "9", "abc", "", "-1", "0")
    assert codigo == 0
    assert sessao.menus_exibidos() == 5
    assert sessao.texto.count("não existe") == 4
    assert "Opção '9' não existe" in sessao.texto


def test_cada_opcao_do_menu_executa_sem_erro(banco):
    codigo, sessao = rodar(
        banco, "1", "", "", "", "2", "3", "4", "5",
        "6", "1", "6", "2", "6", "3", "6", "4", "6", "5", "0",
    )
    assert codigo == 0
    for esperado in ("Top 10 filmes por faturamento", "Gêneros por ROI médio",
                     "Décadas por nota média", "Indicadores gerais",
                     "Comparação por gênero", "Comparação por década",
                     "Orçamento × faturamento: r =", "Orçamento × nota: r =",
                     "encontrado(s)"):
        assert esperado in sessao.texto


def test_correlacao_traz_o_aviso_de_causalidade(banco):
    _, sessao = rodar(banco, "5", "0")
    assert "não implica causalidade" in sessao.texto


# ----------------------------------------------------------------- filtros
def test_filtrar_por_genero(banco):
    _, sessao = rodar(banco, "1", "Terror", "", "", "0")
    assert "8 filme(s) encontrado(s)" in sessao.texto
    assert "Comédia" not in sessao.texto


def test_filtrar_combina_criterios(banco, filmes):
    esperado = sum(1 for f in filmes if "Drama" in f.generos and f.ano >= 2000 and f.nota >= 6)
    _, sessao = rodar(banco, "1", "Drama", "2000", "6", "0")
    assert f"{esperado} filme(s) encontrado(s)" in sessao.texto


def test_filtro_sem_criterios_mostra_os_maiores_faturamentos(banco):
    _, sessao = rodar(banco, "1", "", "", "", "0")
    assert "24 filme(s) encontrado(s); exibindo os 20 de maior faturamento." in sessao.texto


def test_filtro_sem_resultado_avisa_sem_quebrar(banco):
    _, sessao = rodar(banco, "1", "Faroeste", "", "", "0")
    assert "0 filme(s) encontrado(s)" in sessao.texto and "Nenhum filme encontrado." in sessao.texto


def test_filtro_aceita_nota_com_virgula(banco):
    _, sessao = rodar(banco, "1", "", "", "7,5", "0")
    assert "Entrada inválida" not in sessao.texto


@pytest.mark.parametrize("ano, nota, trecho", [
    ("", "abc", "'abc' não é um número"),
    ("xx", "", "'xx' não é um ano válido"),
    ("19.5", "", "'19.5' não é um ano válido"),
    ("", "99", "entre 0 e 10"),
    ("", "-1", "entre 0 e 10"),
    ("", "nan", "entre 0 e 10"),
    ("zz", "yy", "'zz' não é um ano válido"),
])
def test_filtro_com_entrada_invalida_explica_e_volta_ao_menu(banco, ano, nota, trecho):
    codigo, sessao = rodar(banco, "1", "", ano, nota, "0")
    assert codigo == 0 and trecho in sessao.texto
    assert "Nada foi filtrado" in sessao.texto and "Traceback" not in sessao.texto
    assert sessao.menus_exibidos() == 2


def test_filtro_com_dois_erros_lista_os_dois(banco):
    _, sessao = rodar(banco, "1", "", "zz", "yy", "0")
    assert "'zz' não é um ano válido" in sessao.texto and "'yy' não é um número" in sessao.texto


# ---------------------------------------------------------------- rankings
def test_ranking_inexistente_explica_e_volta_ao_menu(banco):
    codigo, sessao = rodar(banco, "6", "9", "0")
    assert codigo == 0 and "Ranking '9' não existe" in sessao.texto
    assert sessao.menus_exibidos() == 2


def test_ranking_de_filmes_traz_o_maior_faturamento_primeiro(banco, filmes):
    maior = max(filmes, key=lambda f: f.faturamento)
    _, sessao = rodar(banco, "6", "1", "0")
    linha_1 = [l for l in sessao.texto.splitlines() if l.startswith("Filme ")][0]
    assert linha_1.startswith(maior.titulo)


# ------------------------------------------------------------- entrada/saída
def test_ler_trata_fim_de_entrada_como_sair(monkeypatch):
    def fim(_prompt):
        raise EOFError
    monkeypatch.setattr("builtins.input", fim)
    assert cli._ler("x") == "0"


def test_ctrl_c_encerra_com_codigo_130(banco):
    def interrompe(_prompt):
        raise KeyboardInterrupt
    saida = []
    codigo = cli.main(["--banco", banco], ler=interrompe, escrever=saida.append)
    assert codigo == 130 and "Encerrado." in "\n".join(saida)


def test_main_usa_o_terminal_por_padrao(banco, monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda _p: "0")
    assert cli.main(["--banco", banco]) == 0
    assert "Base carregada" in capsys.readouterr().out


def test_nao_ha_rede_nem_arquivo_alem_do_banco(banco):
    # a conftest bloqueia sockets: se a CLI tentasse a rede, este teste falharia
    assert rodar(banco, "2", "3", "0")[0] == 0


# ------------------------------------------------- arquivo único: como roda
def test_importar_o_main_nao_executa_nada():
    """Sem `if __name__ == "__main__"`, quem garante isso é a expressão de entrada."""
    r = subprocess.run([sys.executable, "-c", "import main"], cwd=RAIZ,
                       capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout == "" and r.stderr == ""


def test_rodar_como_script_sem_banco_devolve_1_e_explica(tmp_path):
    r = subprocess.run([sys.executable, str(RAIZ / "main.py"), "--banco", str(tmp_path / "x.db")],
                       cwd=tmp_path, capture_output=True, text=True)
    assert r.returncode == 1 and "erro: banco não encontrado" in r.stdout
    assert "python main.py --ingerir" in r.stdout and "Traceback" not in r.stderr


def test_rodar_como_script_abre_o_menu_e_sai_no_fim_da_entrada(banco):
    r = subprocess.run([sys.executable, str(RAIZ / "main.py"), "--banco", banco],
                       input="", capture_output=True, text=True)      # Ctrl-D
    assert r.returncode == 0 and "Base carregada: 24 filmes" in r.stdout and "Até logo!" in r.stdout


def test_ajuda_lista_as_opcoes():
    r = subprocess.run([sys.executable, str(RAIZ / "main.py"), "--help"], capture_output=True, text=True)
    assert r.returncode == 0 and "--banco" in r.stdout and "--ingerir" in r.stdout
