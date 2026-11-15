"""Testes da formatação de texto (issue #11)."""
from types import MappingProxyType

import pytest

from main import Movie, ResumoGrupo
from main import (
    AVISO_CORRELACAO,
    LARGURA_TITULO,
    exibir,
    formatar_comparacao,
    formatar_correlacao,
    formatar_filmes,
    formatar_moeda,
    formatar_nota,
    formatar_percentual,
)

BASE = Movie(
    id=1, titulo="Matrix", ano=1999, orcamento=63_000_000,
    faturamento=463_000_000, nota=8.2,
)


def filme(**campos) -> Movie:
    return BASE._replace(**campos)


def resumo(quantidade: int, **indicadores) -> ResumoGrupo:
    return ResumoGrupo(quantidade, MappingProxyType(indicadores))


# ----------------------------------------------------------------- moeda
def test_moeda_usa_ponto_como_separador_de_milhar():
    assert formatar_moeda(1234567) == "US$ 1.234.567"
    assert formatar_moeda(0) == "US$ 0"
    assert formatar_moeda(999) == "US$ 999"


def test_moeda_arredonda_centavos():
    assert formatar_moeda(1234.4) == "US$ 1.234"
    assert formatar_moeda(1234.6) == "US$ 1.235"


def test_moeda_negativa_leva_o_sinal_na_frente():
    assert formatar_moeda(-1500000) == "-US$ 1.500.000"


def test_moeda_negativa_que_arredonda_para_zero_nao_mostra_sinal():
    assert formatar_moeda(-0.2) == "US$ 0"


# ------------------------------------------------------------ percentual
def test_percentual_usa_virgula_decimal():
    assert formatar_percentual(3.0) == "300,0%"
    assert formatar_percentual(0.1234) == "12,3%"


def test_percentual_none_vira_traco():
    assert formatar_percentual(None) == "-"


def test_percentual_negativo_e_zero():
    assert formatar_percentual(-0.5) == "-50,0%"
    assert formatar_percentual(0.0) == "0,0%"
    assert formatar_percentual(-0.0004) == "0,0%"          # nada de "-0,0%"


def test_percentual_grande_tem_separador_de_milhar():
    assert formatar_percentual(123.4) == "12.340,0%"


# ------------------------------------------------------------------ nota
def test_nota_com_virgula_e_traco_para_ausente():
    assert formatar_nota(7.46) == "7,5"
    assert formatar_nota(None) == "-"
    assert formatar_nota(0.0) == "-"


# --------------------------------------------------------------- filmes
def test_tabela_de_filmes_tem_cabecalho_regua_e_linhas():
    linhas = formatar_filmes((filme(),)).splitlines()
    assert len(linhas) == 3
    assert linhas[0].split() == ["Título", "Ano", "Orçamento", "Faturamento", "ROI", "Nota"]
    assert set(linhas[1]) <= {"-", " "}
    assert "Matrix" in linhas[2] and "US$ 463.000.000" in linhas[2]
    assert "634,9%" in linhas[2] and "8,2" in linhas[2]


def test_todas_as_linhas_tem_a_mesma_largura():
    filmes = (filme(id=1, titulo="A"), filme(id=2, titulo="Um título bem mais longo"),
              filme(id=3, orcamento=5, faturamento=2_000_000_000))
    larguras = {len(linha) for linha in formatar_filmes(filmes).splitlines()}
    assert len(larguras) == 1


def test_largura_da_coluna_acompanha_o_maior_valor():
    curto = formatar_filmes((filme(titulo="A"),)).splitlines()[0]
    longo = formatar_filmes((filme(titulo="A" * 20),)).splitlines()[0]
    assert len(longo) - len(curto) >= 20 - len("Título")


def test_titulo_muito_longo_e_cortado_com_reticencias():
    texto = formatar_filmes((filme(titulo="X" * 100),))
    linha = texto.splitlines()[2]
    assert "…" in linha and "X" * LARGURA_TITULO not in linha


def test_filme_sem_dados_mostra_traco_em_vez_de_zero():
    linha = formatar_filmes((filme(orcamento=0, faturamento=0, nota=0.0),)).splitlines()[2]
    assert "US$ 0" not in linha and linha.count("-") >= 4


def test_sem_filmes_diz_que_nao_achou():
    assert formatar_filmes(()) == "Nenhum filme encontrado."


def test_aceita_gerador_e_nao_depende_de_estado():
    gerador = (filme(id=i) for i in (1, 2))
    primeira = formatar_filmes(gerador)
    assert primeira.count("Matrix") == 2
    assert formatar_filmes((filme(id=i) for i in (1, 2))) == primeira


# ------------------------------------------------------------ comparação
def test_comparacao_mostra_titulo_e_indicadores():
    grupos = (("Terror", resumo(10, roi=2.5, nota=6.04, faturamento=120_000_000)),)
    texto = formatar_comparacao("Por gênero", grupos)
    assert texto.startswith("Por gênero\n==========\n")
    linha = texto.splitlines()[-1]
    assert "Terror" in linha and "10" in linha
    assert "250,0%" in linha and "6,0" in linha and "US$ 120.000.000" in linha


def test_comparacao_indicador_none_ou_ausente_vira_traco():
    grupos = (("Doc", resumo(2, roi=None, nota=8.0)),)        # sem 'faturamento'
    linha = formatar_comparacao("T", grupos).splitlines()[-1]
    assert linha.count("-") >= 2 and "8,0" in linha


def test_comparacao_chave_composta_vira_a_x_b():
    grupos = ((("Ação", 1990), resumo(1, roi=1.0)),)
    assert "Ação × 1990" in formatar_comparacao("T", grupos)


def test_comparacao_alinha_as_colunas():
    grupos = (("A", resumo(1, roi=1.0)), ("Ficção científica", resumo(100, roi=12.0)))
    larguras = {len(l) for l in formatar_comparacao("T", grupos).splitlines()[2:]}
    assert len(larguras) == 1


def test_comparacao_sem_grupos():
    assert "Nenhum grupo para comparar." in formatar_comparacao("Vazio", ())


# ------------------------------------------------------------ correlação
def test_correlacao_mostra_coeficiente_forca_e_aviso():
    texto = formatar_correlacao("Orçamento × faturamento", 0.873)
    assert "r = 0,873" in texto and "forte positiva" in texto
    assert AVISO_CORRELACAO in texto and "não implica causalidade" in texto


@pytest.mark.parametrize("r, esperado", [
    (0.05, "praticamente nula"), (-0.05, "praticamente nula"),
    (0.2, "fraca positiva"), (-0.2, "fraca negativa"),
    (0.5, "moderada positiva"), (-0.5, "moderada negativa"),
    (0.9, "forte positiva"), (-0.9, "forte negativa"), (1.0, "forte positiva"),
])
def test_correlacao_descreve_a_forca_e_o_sentido(r, esperado):
    assert esperado in formatar_correlacao("x", r)


def test_correlacao_none_e_indefinida_e_mantem_o_aviso():
    texto = formatar_correlacao("x", None)
    assert "indefinida" in texto and AVISO_CORRELACAO in texto


# ----------------------------------------------------------------- exibir
def test_exibir_escreve_no_terminal(capsys):
    exibir("olá, mundo")
    assert capsys.readouterr().out == "olá, mundo\n"


def test_formatar_nao_escreve_nada_no_terminal(capsys):
    formatar_filmes((filme(),))
    formatar_comparacao("T", (("A", resumo(1)),))
    formatar_correlacao("x", 0.5)
    assert capsys.readouterr().out == ""
