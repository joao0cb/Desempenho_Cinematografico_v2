"""Análise de desempenho cinematográfico: programa único (main.py).

REGRAS DO TRABALHO
  - nada de `def`, `for`, `while` nem `if` como instrução;
  - `for` e `if` só aparecem DENTRO de lambdas (compreensões e `a if c else b`);
  - todo o código fica neste arquivo.

COMO FOI FEITO
  - cada "função" é uma lambda atribuída a um nome (o nome é resolvido na hora da
    chamada, então uma lambda pode chamar outra definida mais abaixo, ou a si mesma);
  - repetição = map / filter / reduce / compreensões dentro de lambda;
  - sequência de passos = tupla `(passo1, passo2, resultado)[-1]`;
  - variável local = parâmetro de lambda (aplicada na hora) ou `:=` dentro dela;
  - `raise` e `try/except` não cabem em lambda, então viram EXPRESSÕES: `_lancar` e
    `_tentar` (seção 1), sem usar as palavras `raise`, `try`, `with` nem `class`.

SEÇÕES
   1 base: exceções como expressão    8 ranking
   2 domínio                           9 formatação (texto)
   3 dados: conversão e limpeza       10 banco SQLite
   4 métricas e dimensões             11 cliente da API do TMDB
   5 filtros                          12 ingestão (TMDB -> SQLite)
   6 análises (agrupar/comparar)      13 CLI (menu) e ponto de entrada
   7 correlação de Pearson
"""
import argparse
import contextlib
import json
import math
import os
import re
import sqlite3
import sys
import time
from collections import namedtuple
from collections.abc import Iterable, Mapping
from datetime import date
from functools import partial, reduce
from itertools import chain, count, groupby, islice, takewhile
from operator import add, attrgetter, itemgetter, mul
from pathlib import Path
from types import MappingProxyType

import requests
from dotenv import load_dotenv

# ==========================================================================
# 1. BASE: exceções como expressão
# ==========================================================================

# `raise` como expressão: o gerador vazio recebe a exceção no `throw` e a levanta.
_lancar = lambda excecao: (_ for _ in ()).throw(excecao)

# `try/except` como expressão. Um decorador de contexto cujo __exit__ devolve True
# engole a exceção (só dos tipos pedidos); a classe é criada por `type(...)`, sem
# `class`, e os "métodos" são lambdas.
_Armadilha = type("Armadilha", (contextlib.ContextDecorator,), {
    "__init__": lambda self, *tipos: setattr(self, "tipos", tipos),
    "__enter__": lambda self: self,
    "__exit__": lambda self, tipo, valor, rastro: (
        tipo is not None and issubclass(tipo, self.tipos)
        and (setattr(self, "erro", valor) or True)
    ),
})

# _tentar(f, *tipos) -> (True, resultado) ou (False, exceção capturada).
# Exceções de outros tipos continuam subindo normalmente.
_tentar = lambda funcao, *tipos: (
    lambda armadilha: (
        lambda r: (False, armadilha.erro) if r is None else (True, r[0])
    )(armadilha(lambda: (funcao(),))())
)(_Armadilha(*tipos))

# Atalho: resultado de `funcao`, ou `padrao` se ocorrer uma das exceções.
_ou = lambda funcao, padrao, *tipos: (
    lambda ok_valor: ok_valor[1] if ok_valor[0] else padrao
)(_tentar(funcao, *tipos))

# ==========================================================================
# 2. DOMÍNIO: tipos imutáveis compartilhados
# ==========================================================================

# Registro imutável de um filme (uma linha da tabela `filmes`, na mesma ordem).
# Só id, titulo e ano são obrigatórios; o resto tem o valor "desconhecido":
# números 0, textos '', listas ().
Movie = namedtuple(
    "Movie",
    ("id", "titulo", "ano", "data_lancamento", "generos", "orcamento", "faturamento",
     "nota", "popularidade", "duracao", "idioma_original", "classificacao", "diretor",
     "elenco", "paises", "empresas", "colecao"),
    defaults=("", (), 0, 0, 0.0, 0.0, 0, "", "", "", (), (), (), ""),
)

# Indicadores agregados de um grupo: `indicadores` mapeia nome -> média (ou None).
ResumoGrupo = namedtuple("ResumoGrupo", ("quantidade", "indicadores"))

CAMPOS_NUMERICOS = ("ano", "orcamento", "faturamento", "nota", "popularidade", "duracao")
CAMPOS_TEXTO = ("titulo", "data_lancamento", "idioma_original", "classificacao",
                "diretor", "colecao")
CAMPOS_LISTA = ("generos", "elenco", "paises", "empresas")

# ==========================================================================
# 3. DADOS: conversão, validação e limpeza (puras)
# ==========================================================================

ANO_MIN = 1888      # primeiro filme da história
ANO_MAX = 2100      # teto de sanidade
NOTA_MAX = 10.0

# float | None; None se ausente, booleano, ilegível, NaN ou infinito.
_numero = lambda valor: (
    None if valor is None or isinstance(valor, bool)
    else (lambda ok_n: (ok_n[1] if math.isfinite(ok_n[1]) else None) if ok_n[0] else None)(
        _tentar(lambda: float(valor), TypeError, ValueError))
)

# int; 0 se ausente ou ilegível (ex.: '63000000.0' -> 63000000).
_inteiro = lambda valor: (
    valor if isinstance(valor, int) and not isinstance(valor, bool)
    else 0 if (n := _numero(valor)) is None else int(n)
)

# float; 0.0 se ausente ou ilegível.
_decimal = lambda valor: 0.0 if (n := _numero(valor)) is None else n

# str sem espaços nas pontas; '' se ausente.
_texto = lambda valor: "" if valor is None else str(valor).strip()

# 'AAAA-MM-DD'; '' se ausente ou inválida.
_data = lambda valor: _ou(
    lambda: date.fromisoformat(_texto(valor)[:10]).isoformat(), "", ValueError)

# Lista JSON (texto) ou sequência -> tupla de textos, sem vazios e sem repetição.
# Texto que não é lista JSON válida vira () (desconhecido).
_lista = lambda valor: (
    lambda itens: tuple(dict.fromkeys(filter(None, map(_texto, itens))))
    if isinstance(itens, (list, tuple)) else ()
)(_ou(lambda: json.loads(valor), (), ValueError) if isinstance(valor, str) else valor)

_CONVERSOR_POR_CAMPO = {
    "id": _inteiro, "titulo": _texto, "ano": _inteiro, "data_lancamento": _data,
    "generos": _lista, "orcamento": _inteiro, "faturamento": _inteiro,
    "nota": _decimal, "popularidade": _decimal, "duracao": _inteiro,
    "idioma_original": _texto, "classificacao": _texto, "diretor": _texto,
    "elenco": _lista, "paises": _lista, "empresas": _lista, "colecao": _texto,
}
# Na ordem dos campos de Movie; um campo novo sem conversor falha já no import.
_CONVERSORES = tuple(map(_CONVERSOR_POR_CAMPO.__getitem__, Movie._fields))
_CAMPOS_LISTA = frozenset(CAMPOS_LISTA)

# Linha do banco (colunas na ordem de Movie) -> Movie; None se a linha não serve.
linha_para_movie = lambda linha: (
    None if not isinstance(linha, Iterable)
    else (lambda valores: (
        None if len(valores) != len(_CONVERSORES) or _numero(valores[0]) is None
        else Movie(*map(lambda converter, valor: converter(valor), _CONVERSORES, valores))
    ))(tuple(linha))
)

# Movie -> linha para o banco (campos-lista viram texto JSON).
filme_para_linha = lambda filme: tuple(
    json.dumps(list(valor), ensure_ascii=False) if campo in _CAMPOS_LISTA else valor
    for campo, valor in zip(Movie._fields, filme)
)

# Linhas -> Movies sob demanda, pulando as inutilizáveis.
para_filmes = lambda linhas: (
    filme for filme in map(linha_para_movie, linhas) if filme is not None
)

_objeto = lambda valor: valor if isinstance(valor, Mapping) else {}
_itens = lambda valor: (
    tuple(item for item in valor if isinstance(item, Mapping))
    if isinstance(valor, (list, tuple)) else ()
)
_nota_limitada = lambda valor: min(max(_decimal(valor), 0.0), NOTA_MAX)
_primeiro_diretor = lambda creditos: next(
    (_texto(pessoa.get("name")) for pessoa in _itens(creditos.get("crew"))
     if pessoa.get("job") == "Director"), "")
_classificacao = lambda release_dates: next(
    (_texto(data.get("certification"))
     for pais in _itens(_objeto(release_dates).get("results"))
     if pais.get("iso_3166_1") == "US"
     for data in _itens(pais.get("release_dates"))
     if _texto(data.get("certification"))), "")

# JSON de /movie/{id}?append_to_response=credits,release_dates -> Movie (ou None
# se faltar id, título ou data de lançamento). Usada pela ingestão.
para_movie = lambda detalhe: (
    None if not isinstance(detalhe, Mapping)
    else (lambda filme_id, titulo, data_lancamento, creditos: (
        None if filme_id <= 0 or not titulo or not data_lancamento
        else Movie(
            id=filme_id,
            titulo=titulo,
            ano=_inteiro(data_lancamento[:4]),
            data_lancamento=data_lancamento,
            generos=_lista([item.get("name") for item in _itens(detalhe.get("genres"))]),
            orcamento=_inteiro(detalhe.get("budget")),
            faturamento=_inteiro(detalhe.get("revenue")),
            nota=_nota_limitada(detalhe.get("vote_average")),
            popularidade=_decimal(detalhe.get("popularity")),
            duracao=_inteiro(detalhe.get("runtime")),
            idioma_original=_texto(detalhe.get("original_language")),
            classificacao=_classificacao(detalhe.get("release_dates")),
            diretor=_primeiro_diretor(creditos),
            elenco=_lista([item.get("name") for item in _itens(creditos.get("cast"))[:5]]),
            paises=_lista([item.get("iso_3166_1")
                           for item in _itens(detalhe.get("production_countries"))]),
            empresas=_lista([item.get("name")
                             for item in _itens(detalhe.get("production_companies"))]),
            colecao=_texto(_objeto(detalhe.get("belongs_to_collection")).get("name")),
        )
    ))(_inteiro(detalhe.get("id")), _texto(detalhe.get("title")),
       _data(detalhe.get("release_date")), _objeto(detalhe.get("credits")))
)

# Predicados de validação (Movie -> bool).
_presente = lambda valor: (
    valor > 0 if isinstance(valor, (int, float)) and not isinstance(valor, bool)
    else bool(valor)
)
# Predicado "o filme tem o campo preenchido"; ValueError se o campo não existir.
tem_valor = lambda campo: (
    _lancar(ValueError(f"campo desconhecido: {campo!r}")) if campo not in Movie._fields
    else (lambda filme: _presente(getattr(filme, campo)))
)
tem_orcamento_valido = lambda filme: filme.orcamento > 0
tem_faturamento_valido = lambda filme: filme.faturamento > 0
tem_financeiro_valido = lambda filme: tem_orcamento_valido(filme) and tem_faturamento_valido(filme)
tem_nota_valida = lambda filme: 0.0 < filme.nota <= NOTA_MAX
tem_ano_valido = lambda filme: ANO_MIN <= filme.ano <= ANO_MAX

REQUISITOS_PADRAO = (tem_orcamento_valido, tem_faturamento_valido, tem_nota_valida, tem_ano_valido)
eh_valido = lambda filme: all(requisito(filme) for requisito in REQUISITOS_PADRAO)

# Limpeza preguiçosa: só os filmes que cumprem TODAS as exigências dadas.
limpar_com = lambda exigencias, filmes: (
    lambda fixas: filter(lambda filme: all(e(filme) for e in fixas), filmes)
)(tuple(exigencias))
limpar = lambda filmes: limpar_com(REQUISITOS_PADRAO, filmes)
com_financeiro_valido = lambda filmes: filter(tem_financeiro_valido, filmes)
com_nota_valida = lambda filmes: filter(tem_nota_valida, filmes)
# Os `n` filmes a partir de `inicio`, sem materializar o resto.
amostra = lambda filmes, n, inicio=0: islice(filmes, inicio, inicio + n)

# ==========================================================================
# 4. MÉTRICAS E DIMENSÕES (puras)
#    MÉTRICA  (Seletor):  Movie -> número ou None   (médias, rankings, correlação)
#    DIMENSÃO (Dimensao): Movie -> chaves de grupo  (agrupamentos, comparações)
# ==========================================================================

NOMES_METRICAS = ("orcamento", "faturamento", "lucro", "roi", "nota",
                  "popularidade", "duracao", "ano")
NOMES_DIMENSOES = ("genero", "decada", "ano", "mes", "trimestre", "idioma", "classificacao",
                   "diretor", "ator", "pais", "empresa", "colecao", "em_franquia",
                   "faixa_orcamento", "faixa_nota", "faixa_duracao")

decada = lambda filme: (filme.ano // 10) * 10
# Mês (1 a 12) a partir de data_lancamento; None se desconhecido.
mes = lambda filme: (
    None if len(filme.data_lancamento) < 7 or not filme.data_lancamento[5:7].isdecimal()
    else (numero if 1 <= (numero := int(filme.data_lancamento[5:7])) <= 12 else None)
)
trimestre = lambda filme: None if (m := mes(filme)) is None else (m - 1) // 3 + 1
lucro = lambda filme: filme.faturamento - filme.orcamento
# (faturamento - orcamento) / orcamento; None se orcamento <= 0.
roi = lambda filme: (
    None if filme.orcamento <= 0 else (filme.faturamento - filme.orcamento) / filme.orcamento
)

# Agregações por reduce.
soma = lambda valores: float(reduce(add, valores, 0.0))
# Média em uma passada: o acumulador é a tupla (soma, contagem).
media = lambda valores: (
    lambda total_e_quantidade: None if total_e_quantidade[1] == 0
    else total_e_quantidade[0] / total_e_quantidade[1]
)(reduce(lambda acc, valor: (acc[0] + valor, acc[1] + 1), valores, (0.0, 0)))
# Mediana com `sorted` (lista nova: a entrada nunca é alterada).
mediana = lambda valores: (
    lambda o: (
        None if not o
        else float(o[len(o) // 2]) if len(o) % 2 == 1
        else (float(o[len(o) // 2 - 1]) + float(o[len(o) // 2])) / 2
    )
)(sorted(valores))

# 0 é o "não informado" do TMDB: vira None para não poluir médias e rankings.
_positivo = lambda valor: valor if valor > 0 else None

_SELETORES = MappingProxyType({
    "orcamento": lambda filme: _positivo(float(filme.orcamento)),
    "faturamento": lambda filme: _positivo(float(filme.faturamento)),
    "lucro": lambda filme: float(lucro(filme)),
    "roi": roi,
    "nota": lambda filme: _positivo(filme.nota),
    "popularidade": lambda filme: _positivo(filme.popularidade),
    "duracao": lambda filme: _positivo(float(filme.duracao)),
    "ano": lambda filme: _positivo(float(filme.ano)),
})
# Seletor da métrica `nome`; ValueError se desconhecida.
metrica = lambda nome: (
    _SELETORES[nome] if nome in _SELETORES
    else _lancar(ValueError(f"métrica desconhecida: {nome!r}; use uma de {NOMES_METRICAS}"))
)

# Chave única: tupla de 1 elemento; valor "desconhecido" fica fora do agrupamento.
_uma = lambda chave: () if chave is None or chave == "" or chave == 0 else (chave,)

# Faixas: (limite superior exclusivo, rótulo) + rótulo da última faixa.
FAIXAS_ORCAMENTO = ((10_000_000, "até US$ 10 mi"), (50_000_000, "US$ 10-50 mi"),
                    (100_000_000, "US$ 50-100 mi"))
FAIXA_ORCAMENTO_FINAL = "acima de US$ 100 mi"
FAIXAS_NOTA = ((5.0, "0-5"), (7.0, "5-7"), (8.0, "7-8"))
FAIXA_NOTA_FINAL = "8-10"
FAIXAS_DURACAO = ((90, "até 90 min"), (120, "90-120 min"), (150, "120-150 min"))
FAIXA_DURACAO_FINAL = "150 min ou mais"

# Primeira faixa cujo limite superior é maior que o valor; senão, a última.
_faixa = lambda valor, faixas, ultima: (
    next((nome for limite, nome in faixas if valor < limite), ultima),
)

_DIMENSOES = MappingProxyType({
    "genero": lambda filme: tuple(filme.generos),
    "decada": lambda filme: _uma(decada(filme)),
    "ano": lambda filme: _uma(filme.ano),
    "mes": lambda filme: _uma(mes(filme)),
    "trimestre": lambda filme: _uma(trimestre(filme)),
    "idioma": lambda filme: _uma(filme.idioma_original),
    "classificacao": lambda filme: _uma(filme.classificacao),
    "diretor": lambda filme: _uma(filme.diretor),
    "ator": lambda filme: tuple(filme.elenco),
    "pais": lambda filme: tuple(filme.paises),
    "empresa": lambda filme: tuple(filme.empresas),
    "colecao": lambda filme: _uma(filme.colecao),
    "em_franquia": lambda filme: ("em franquia" if filme.colecao else "fora de franquia",),
    "faixa_orcamento": lambda filme: (
        () if filme.orcamento <= 0
        else _faixa(filme.orcamento, FAIXAS_ORCAMENTO, FAIXA_ORCAMENTO_FINAL)),
    "faixa_nota": lambda filme: (
        () if filme.nota <= 0 else _faixa(filme.nota, FAIXAS_NOTA, FAIXA_NOTA_FINAL)),
    "faixa_duracao": lambda filme: (
        () if filme.duracao <= 0
        else _faixa(filme.duracao, FAIXAS_DURACAO, FAIXA_DURACAO_FINAL)),
})
# Dimensão `nome`; tupla vazia quando o filme não tem o dado. ValueError se desconhecida.
dimensao = lambda nome: (
    _DIMENSOES[nome] if nome in _DIMENSOES
    else _lancar(ValueError(f"dimensão desconhecida: {nome!r}; use uma de {NOMES_DIMENSOES}"))
)

# ==========================================================================
# 5. FILTROS: funções de alta ordem que fabricam predicados (Movie -> bool)
# ==========================================================================

# Nome de campo errado estoura aqui, não silenciosamente.
_exigir_campo = lambda campo, permitidos, tipo: (
    None if campo in permitidos
    else _lancar(ValueError(f"campo não {tipo}: {campo!r}; use um de {tuple(permitidos)}"))
)

# Gênero entre os do filme (sem diferenciar maiúsculas).
por_genero = lambda genero: (
    lambda alvo: lambda filme: any(item.casefold() == alvo for item in filme.generos)
)(genero.strip().casefold())
# Década informada (1994 -> 1990..1999); ano desconhecido (0) nunca casa.
por_decada = lambda decada_: (
    lambda inicio: lambda filme: filme.ano > 0 and inicio <= filme.ano <= inicio + 9
)((decada_ // 10) * 10)
# Década informada em diante.
a_partir_da_decada = lambda decada_: (
    lambda inicio: lambda filme: filme.ano > 0 and filme.ano >= inicio
)((decada_ // 10) * 10)
# minimo <= campo <= maximo para um campo NUMÉRICO (None = sem limite).
por_faixa = lambda campo, minimo=None, maximo=None: (
    _exigir_campo(campo, CAMPOS_NUMERICOS, "numérico"),
    lambda filme: (
        (minimo is None or getattr(filme, campo) >= minimo)
        and (maximo is None or getattr(filme, campo) <= maximo)),
)[-1]
orcamento_minimo = lambda valor: por_faixa("orcamento", minimo=valor)
faturamento_minimo = lambda valor: por_faixa("faturamento", minimo=valor)
nota_minima = lambda valor: por_faixa("nota", minimo=valor)
# Campo de valor único igual a algum dos valores.
por_valor = lambda campo, *valores: (
    _exigir_campo(campo, CAMPOS_TEXTO + CAMPOS_NUMERICOS, "de valor único"),
    (lambda alvos: lambda filme: getattr(filme, campo) in alvos)(frozenset(valores)),
)[-1]
# Campo-LISTA contém algum dos valores (sem diferenciar maiúsculas).
contendo = lambda campo, *valores: (
    _exigir_campo(campo, CAMPOS_LISTA, "lista"),
    (lambda alvos: lambda filme: any(
        item.casefold() in alvos for item in getattr(filme, campo)
    ))(frozenset(valor.strip().casefold() for valor in valores)),
)[-1]

# Combinadores: recebem predicados e devolvem predicado.
todos = lambda *predicados: lambda filme: all(p(filme) for p in predicados)
combinar = lambda *predicados: todos(*predicados)
qualquer = lambda *predicados: lambda filme: any(p(filme) for p in predicados)
negar = lambda predicado: lambda filme: not predicado(filme)
filtrar = lambda predicado, filmes: filter(predicado, filmes)

_fabrica_de_faixa = lambda campo, limite: lambda valor: por_faixa(campo, **{limite: valor})
_FAIXAS = dict(chain(
    map(lambda campo: (f"{campo}_min", _fabrica_de_faixa(campo, "minimo")), CAMPOS_NUMERICOS),
    map(lambda campo: (f"{campo}_max", _fabrica_de_faixa(campo, "maximo")), CAMPOS_NUMERICOS),
))
_CRITERIOS = MappingProxyType({
    **_FAIXAS,
    "genero": por_genero,
    "decada": por_decada,
    "a_partir_de": a_partir_da_decada,
    "ano": lambda valor: por_valor("ano", valor),
    "idioma": lambda valor: por_valor("idioma_original", valor),
    "classificacao": lambda valor: por_valor("classificacao", valor),
    "diretor": lambda valor: por_valor("diretor", valor),
    "colecao": lambda valor: por_valor("colecao", valor),
    "ator": lambda valor: contendo("elenco", valor),
    "pais": lambda valor: contendo("paises", valor),
    "empresa": lambda valor: contendo("empresas", valor),
})
# Predicado pronto a partir de critérios nomeados (combinados com E).
# Ex.: make_filtro(genero='Terror', a_partir_de=2010, nota_min=7)
make_filtro = lambda **criterios: (
    todos(*map(lambda par: _CRITERIOS[par[0]](par[1]), criterios.items()))
    if all(nome in _CRITERIOS for nome in criterios)
    else _lancar(ValueError(
        f"critério desconhecido: {next(n for n in criterios if n not in _CRITERIOS)!r}; "
        f"use um de {tuple(sorted(_CRITERIOS))}"))
)

# ==========================================================================
# 6. ANÁLISES: agrupamento e comparação de grupos (puras, sem SQL)
# ==========================================================================

# Indicadores calculados por `resumir` (nomes do catálogo `metrica`).
INDICADORES_PADRAO = ("roi", "nota", "faturamento")

# Um par (chave, filme) para CADA chave da dimensão; dict.fromkeys tira repetições.
_pares_chave_filme = lambda dim, filmes: chain.from_iterable(map(
    lambda filme: map(lambda chave: (chave, filme), dict.fromkeys(dim(filme))), filmes))

# Agrupa pelas chaves de uma Dimensao (um filme pode entrar em vários grupos).
# `sorted` devolve lista nova e é estável; `groupby` só junta vizinhos.
agrupar_por = lambda dim, filmes: dict(map(
    lambda grupo: (grupo[0], tuple(map(itemgetter(1), grupo[1]))),
    groupby(sorted(_pares_chave_filme(dim, filmes), key=itemgetter(0)), key=itemgetter(0)),
))

# quantidade + média de cada indicador (só filmes com dado válido; None se nenhum).
resumir = lambda filmes, indicadores=INDICADORES_PADRAO: (
    lambda base: ResumoGrupo(len(base), MappingProxyType(dict(map(
        lambda nome: (nome, media(filter(lambda v: v is not None, map(metrica(nome), base)))),
        indicadores,
    ))))
)(tuple(filmes))

comparar_por = lambda dim, filmes: (
    lambda grupos: MappingProxyType(
        dict(map(lambda par: (par[0], resumir(par[1])), grupos.items())))
)(agrupar_por(dim, filmes))
comparar_por_genero = lambda filmes: comparar_por(dimensao("genero"), filmes)
comparar_por_decada = lambda filmes: comparar_por(dimensao("decada"), filmes)

# ==========================================================================
# 7. CORRELAÇÃO DE PEARSON (pura)
#    Correlação não implica causalidade: a interpretação é de quem lê.
# ==========================================================================

compor = lambda f, g: lambda x: f(g(x))
_desvios = lambda valores: (
    lambda media_da_serie: tuple(map(lambda valor: valor - media_da_serie, valores))
)(soma(valores) / len(valores))

# Coeficiente de Pearson; None se houver menos de 2 pares ou série constante.
pearson = lambda xs, ys: (
    lambda pares: (
        lambda valores_x, valores_y: (
            None if len(pares) < 2 or len(frozenset(valores_x)) < 2 or len(frozenset(valores_y)) < 2
            else (lambda desvios_x, desvios_y: (
                # O limitador absorve erro de ponto flutuante (ex.: 1.0000000000000002).
                max(-1.0, min(1.0, soma(map(mul, desvios_x, desvios_y)) / math.sqrt(
                    soma(map(lambda d: d * d, desvios_x)) * soma(map(lambda d: d * d, desvios_y)))))
            ))(_desvios(valores_x), _desvios(valores_y))
        )
    )(tuple(map(itemgetter(0), pares)), tuple(map(itemgetter(1), pares)))
)(tuple(zip(xs, ys)))

# Correlação entre dois seletores, descartando filmes em que algum devolve None.
correlacionar = lambda x, y, filmes: (
    lambda validos: pearson(tuple(map(itemgetter(0), validos)), tuple(map(itemgetter(1), validos)))
)(tuple(filter(lambda par: None not in par, map(lambda filme: (x(filme), y(filme)), filmes))))

# Pares (orçamento, faturamento) válidos, sob demanda.
pares_orcamento_faturamento = lambda filmes: (
    (float(filme.orcamento), float(filme.faturamento))
    for filme in filmes if filme.orcamento > 0 and filme.faturamento > 0
)
# O TMDB usa 0 para 'não informado': para análise, zero equivale a ausente.
_positivo_ou_none = lambda valor: valor if valor is not None and valor > 0 else None
_sel_orcamento = compor(_positivo_ou_none, attrgetter("orcamento"))
_sel_faturamento = compor(_positivo_ou_none, attrgetter("faturamento"))
_sel_nota = compor(_positivo_ou_none, attrgetter("nota"))
orcamento_vs_faturamento = lambda filmes: correlacionar(_sel_orcamento, _sel_faturamento, filmes)
orcamento_vs_nota = lambda filmes: correlacionar(_sel_orcamento, _sel_nota, filmes)

# Texto com coeficiente, pares usados e aviso de causalidade (devolve str; não imprime).
relatorio_correlacao = lambda filmes: (
    lambda pares: (
        lambda r: "\n".join((
            "Correlação orçamento x faturamento",
            f"  pares usados: {len(pares)}",
            f"  coeficiente de Pearson: {'-' if r is None else f'{r:.3f}'}",
            *(("  (não calculável: menos de 2 pares ou uma das séries é constante)",)
              if r is None else ()),
            "  Correlação não implica causalidade: um coeficiente alto não prova "
            "que o orçamento causou o faturamento.",
        ))
    )(pearson(tuple(map(itemgetter(0), pares)), tuple(map(itemgetter(1), pares))))
)(tuple(pares_orcamento_faturamento(filmes)))

# ==========================================================================
# 8. RANKING (puro)
# ==========================================================================

# Os n primeiros filmes pela chave (qualquer métrica). Chave None fica de fora; a chave
# é calculada UMA vez por filme; `sorted` é estável (empates mantêm a ordem de entrada);
# n <= 0 devolve ().
top_filmes = lambda n, chave, filmes, decrescente=True: tuple(islice(
    map(itemgetter(1), sorted(
        filter(lambda par: par[0] is not None, map(lambda filme: (chave(filme), filme), filmes)),
        key=itemgetter(0), reverse=decrescente)),
    max(n, 0)))
# Os n primeiros grupos por um indicador do resumo; indicador None/ausente fica de fora.
top_grupos = lambda n, indicador, grupos, decrescente=True: tuple(islice(
    sorted(filter(lambda par: par[1].indicadores.get(indicador) is not None, grupos.items()),
           key=lambda par: par[1].indicadores[indicador], reverse=decrescente),
    max(n, 0)))

# ==========================================================================
# 9. FORMATAÇÃO: texto para o terminal (as `formatar_*` são puras; só `exibir` escreve)
# ==========================================================================

AVISO_CORRELACAO = "Atenção: correlação não implica causalidade."
LARGURA_TITULO = 34
SEPARADOR_COLUNAS = "  "
_PT_BR = str.maketrans({",": ".", ".": ","})   # troca simultânea: 1,234.5 -> 1.234,5
_FORCAS = ((0.1, "praticamente nula"), (0.3, "fraca"), (0.7, "moderada"))
_FORCA_FINAL = "forte"

# Número no padrão brasileiro: milhar '.' e decimal ','.
_num_br = lambda valor, casas: f"{valor:,.{casas}f}".translate(_PT_BR)
# 1234567 -> 'US$ 1.234.567'; negativo -> '-US$ 1.234.567'.
formatar_moeda = lambda valor: (
    lambda inteiro: f"{'-' if inteiro < 0 else ''}US$ {_num_br(abs(inteiro), 0)}"
)(round(valor))
# 3.0 -> '300,0%'; None -> '-'  (o `+ 0.0` troca -0.0 por 0.0).
formatar_percentual = lambda valor: (
    "-" if valor is None else f"{_num_br(round(valor * 100, 1) + 0.0, 1)}%")
# 7.46 -> '7,5'; None ou 0 (sem avaliações) -> '-'.
formatar_nota = lambda valor: "-" if not valor else _num_br(valor, 1)
_ou_traco = lambda valor, formatador: "-" if not valor else formatador(valor)
_cortar = lambda texto, largura: texto if len(texto) <= largura else texto[: largura - 1] + "…"

# Uma linha da tabela; `alinhamento` tem '<' ou '>' por coluna.
_montar = lambda celulas, larguras, alinhamento: SEPARADOR_COLUNAS.join(
    map(lambda c, w, a: f"{c:{a}{w}}", celulas, larguras, alinhamento))
# Tabela com a largura de cada coluna = a do maior texto dela (cabeçalho incluído).
# `zip(cabecalho, *corpo)` transpõe a tabela: cada elemento é uma coluna.
_tabela = lambda cabecalho, linhas, alinhamento: (
    lambda corpo: (
        lambda larguras: "\n".join((
            _montar(cabecalho, larguras, alinhamento),
            SEPARADOR_COLUNAS.join(map(lambda w: "-" * w, larguras)),
            *map(lambda linha: _montar(linha, larguras, alinhamento), corpo),
        ))
    )(tuple(map(lambda coluna: max(map(len, coluna)), zip(cabecalho, *corpo))))
)(tuple(linhas))

_linha_filme = lambda filme: (
    _cortar(filme.titulo, LARGURA_TITULO),
    str(filme.ano),
    _ou_traco(filme.orcamento, formatar_moeda),
    _ou_traco(filme.faturamento, formatar_moeda),
    formatar_percentual(roi(filme)),
    formatar_nota(filme.nota),
)
formatar_filmes = lambda filmes: (
    lambda lista: "Nenhum filme encontrado." if not lista
    else _tabela(("Título", "Ano", "Orçamento", "Faturamento", "ROI", "Nota"),
                 map(_linha_filme, lista), "<>>>>>")
)(tuple(filmes))

# Chave de grupo como texto; chave composta (tupla) vira 'a × b'.
_rotulo = lambda chave: " × ".join(map(str, chave)) if isinstance(chave, tuple) else str(chave)
_linha_grupo = lambda chave, resumo: (
    _rotulo(chave),
    str(resumo.quantidade),
    formatar_percentual(resumo.indicadores.get("roi")),
    formatar_nota(resumo.indicadores.get("nota")),
    _ou_traco(resumo.indicadores.get("faturamento"), formatar_moeda),
)
formatar_comparacao = lambda titulo, grupos: (
    lambda lista, cabecalho: (
        f"{cabecalho}\nNenhum grupo para comparar." if not lista
        else cabecalho + "\n" + _tabela(
            ("Grupo", "Filmes", "ROI médio", "Nota média", "Faturamento médio"),
            map(lambda par: _linha_grupo(*par), lista), "<>>>>")
    )
)(tuple(grupos), titulo + "\n" + "=" * len(titulo))

# Força e sentido em palavras (|r| < 0,1 não tem sentido a descrever).
_descrever = lambda r: (
    lambda forca, sentido: f"correlação {forca}{sentido}"
)(next((nome for limite, nome in _FORCAS if abs(r) < limite), _FORCA_FINAL),
  "" if abs(r) < _FORCAS[0][0] else (" positiva" if r > 0 else " negativa"))
formatar_correlacao = lambda rotulo, r: (
    f"{rotulo}: indefinida (dados insuficientes ou sem variação)\n{AVISO_CORRELACAO}" if r is None
    else f"{rotulo}: r = {_num_br(r, 3)} ({_descrever(r)})\n{AVISO_CORRELACAO}"
)

# A única função com efeito colateral desta seção.
exibir = lambda texto: print(texto)

# ==========================================================================
# 10. BANCO SQLITE (I/O). O banco é só ARMAZENAMENTO: sem GROUP BY/AVG/SUM.
# ==========================================================================

DB_PADRAO = Path("data") / "cinema.db"
SCHEMA_PATH = Path(__file__).with_name("schema.sql")
_COLUNAS = ", ".join(Movie._fields)
_PLACEHOLDERS = ", ".join("?" * len(Movie._fields))
CONSULTA_FILMES = f"SELECT {_COLUNAS} FROM filmes ORDER BY id"

# Abre (e cria, se preciso) o banco. Aceita ':memory:' para testes.
conectar = lambda caminho=DB_PADRAO: (
    lambda destino: (
        None if destino == ":memory:" else Path(destino).parent.mkdir(parents=True, exist_ok=True),
        sqlite3.connect(destino),
    )[-1]
)(str(caminho))
inicializar_schema = lambda conn: (
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8")), None)[-1]
# Insere ou substitui por id; devolve quantos foram gravados.
inserir_filmes = lambda conn, filmes: (
    lambda antes: (
        conn.executemany(
            f"INSERT OR REPLACE INTO filmes ({_COLUNAS}) VALUES ({_PLACEHOLDERS})",
            map(filme_para_linha, filmes)),
        conn.commit(),
        conn.total_changes - antes,
    )[-1]
)(conn.total_changes)
# Streaming: o cursor só é aberto na primeira linha pedida (nada de fetchall()).
ler_linhas = lambda conn: (linha for _ in (0,) for linha in conn.execute(CONSULTA_FILMES))
ler_filmes = lambda conn: para_filmes(ler_linhas(conn))

# ==========================================================================
# 11. CLIENTE DA API DO TMDB (I/O: rede). Mensagens nunca incluem o token.
# ==========================================================================

BASE_URL = "https://api.themoviedb.org/3"
TIMEOUT_PADRAO = 10.0          # segundos antes de desistir de uma requisição
TENTATIVAS_RATE_LIMIT = 2      # novas tentativas depois de um 429
ESPERA_RATE_LIMIT = 2.0        # espera padrão (s) quando não há Retry-After
ESPERA_MAXIMA = 10.0           # teto da espera

# Lê TMDB_TOKEN do ambiente/.env; RuntimeError se ausente.
carregar_token = lambda: (
    load_dotenv(),
    (lambda token: token if token else _lancar(RuntimeError(
        "TMDB_TOKEN não configurado. Copie .env.example para .env e preencha "
        "com o 'Token de Acesso de Leitura da API' "
        "(TMDB > Configurações > API). O .env não deve ser commitado.")))(
        os.environ.get("TMDB_TOKEN", "").strip()),
)[-1]
# Bearer token no header: o token nunca vai na URL (evita vazar em log).
_cabecalhos = lambda token: {"Authorization": f"Bearer {token}", "Accept": "application/json"}
# Respeita o `Retry-After` quando o TMDB manda; senão usa o padrão.
_espera_da_resposta = lambda resposta: (
    lambda ok_n: min(max(ok_n[1], 0.0), ESPERA_MAXIMA) if ok_n[0] else ESPERA_RATE_LIMIT
)(_tentar(lambda: float(resposta.headers.get("Retry-After", "")), TypeError, ValueError))

# GET com timeout; falhas de rede viram RuntimeError com mensagem clara.
_requisitar = lambda token, caminho, parametros: (
    lambda ok_r: (
        ok_r[1] if ok_r[0]
        else _lancar(RuntimeError(
            f"tempo esgotado ao consultar o TMDB ({caminho}) após "
            f"{TIMEOUT_PADRAO:.0f}s. Verifique a conexão e tente de novo."))
        if isinstance(ok_r[1], requests.Timeout)
        else _lancar(RuntimeError(
            f"falha de rede ao consultar o TMDB ({caminho}): {type(ok_r[1]).__name__}."))
    )
)(_tentar(lambda: requests.get(
    f"{BASE_URL}{caminho}", headers=_cabecalhos(token), params=parametros,
    timeout=TIMEOUT_PADRAO), requests.RequestException))

# GET autenticado com tratamento de 401/429 (a nova tentativa é uma chamada recursiva).
_obter = lambda token, caminho, parametros, tentativa=0: (
    lambda resposta: (
        _lancar(RuntimeError(
            "token do TMDB recusado (401). Confira TMDB_TOKEN no .env: "
            "ele precisa ser o 'Token de Acesso de Leitura da API' (começa com 'eyJ')."))
        if resposta.status_code == 401
        else (
            _lancar(RuntimeError(
                "limite de requisições do TMDB atingido (429). "
                "Aguarde alguns instantes e rode a ingestão novamente."))
            if tentativa == TENTATIVAS_RATE_LIMIT
            else (time.sleep(_espera_da_resposta(resposta)),
                  _obter(token, caminho, parametros, tentativa + 1))[-1]
        ) if resposta.status_code == 429
        else _lancar(RuntimeError(f"o TMDB respondeu {resposta.status_code} em {caminho}."))
        if not resposta.ok
        else resposta.json()
    )
)(_requisitar(token, caminho, parametros))

# Uma página de /discover/movie: maior faturamento primeiro, >= 100 votos, pt-BR.
# Esses critérios definem quais filmes entram na base (viés de amostragem).
descobrir_filmes = lambda token, pagina=1: _obter(
    token, "/discover/movie",
    {"sort_by": "revenue.desc", "vote_count.gte": 100, "language": "pt-BR", "page": pagina})
# /movie/{id} com credits e release_dates (único endpoint com budget e revenue).
detalhar_filme = lambda token, filme_id: _obter(
    token, f"/movie/{filme_id}",
    {"language": "pt-BR", "append_to_response": "credits,release_dates"})
buscar_pagina = lambda pagina=1: list(descobrir_filmes(carregar_token(), pagina).get("results", []))
buscar_detalhes = lambda filme_id: dict(detalhar_filme(carregar_token(), filme_id))

# ==========================================================================
# 12. INGESTÃO: TMDB -> SQLite (I/O: rede + banco)
#     Grava página a página: se a rede cair no meio, o que já foi gravado fica.
# ==========================================================================

POR_PAGINA = 20                # filmes por página do /discover/movie
LIMITE_PADRAO = 400
ResumoIngestao = namedtuple("ResumoIngestao", ("buscados", "gravados", "descartados"))

# Quantas páginas são necessárias para `limite` filmes (mínimo 1).
paginas_para = lambda limite: max(1, -(-limite // POR_PAGINA))
# Ids válidos de uma página do discover (ignora entradas sem id).
_ids_da_pagina = lambda token, pagina: tuple(
    id_ for id_ in (item.get("id") for item in descobrir_filmes(token, pagina).get("results", ())
                    if isinstance(item, dict))
    if isinstance(id_, int) and id_ > 0)
# (ids encontrados, filmes convertidos) de uma página; `para_movie` rejeita os incompletos.
_pagina = lambda token, pagina: (
    lambda ids: (ids, tuple(
        filme for filme in map(para_movie, (detalhar_filme(token, id_) for id_ in ids))
        if filme is not None))
)(_ids_da_pagina(token, pagina))
# Gera (lazy) Movies válidos; cada página só é buscada quando a anterior acabou.
coletar_filmes = lambda token, paginas: (
    filme for pagina in range(1, paginas + 1) for filme in _pagina(token, pagina)[1])

# Soma as páginas até o TMDB não ter mais nenhuma (takewhile) e grava cada uma.
_gravar_paginas = lambda conn, token, paginas: (
    lambda totais: ResumoIngestao(totais[0], totais[2], totais[0] - totais[1])
)(reduce(
    lambda acc, pagina: (acc[0] + len(pagina[0]), acc[1] + len(pagina[1]),
                         acc[2] + inserir_filmes(conn, pagina[1])),
    takewhile(lambda pagina: pagina[0], map(lambda n: _pagina(token, n), range(1, paginas + 1))),
    (0, 0, 0)))
# Abre o banco, grava e FECHA a conexão mesmo se algo falhar no meio (e re-levanta).
_ingerir = lambda token, paginas, caminho_db: (
    lambda conn: (
        lambda ok_r: (conn.close(), ok_r[1] if ok_r[0] else _lancar(ok_r[1]))[-1]
    )(_tentar(lambda: (inicializar_schema(conn), _gravar_paginas(conn, token, paginas))[-1],
              BaseException))
)(conectar(caminho_db))
# Cria o schema, coleta e grava; imprime o resumo e devolve quantos foram gravados.
ingerir = lambda paginas=25, caminho_db=DB_PADRAO: (
    lambda resumo: (
        print(f"{caminho_db}: {resumo.buscados} buscados, {resumo.gravados} gravados, "
              f"{resumo.descartados} descartados (sem id, título ou data de lançamento)."),
        resumo.gravados,
    )[-1]
)(_ingerir(carregar_token(), paginas, caminho_db))

# ==========================================================================
# 13. CLI (I/O: terminal). Cada opção do menu é uma função `filmes -> texto` que
#     COMPÕE as funções acima; só o laço que lê a escolha toca o terminal.
# ==========================================================================

TOP_N = 10
LIMITE_LISTAGEM = 20
SAIDAS = frozenset({"0", "sair", "s"})
COMANDO_INGESTAO = "python main.py --ingerir"

# Seleciona, transforma (descartando None) e agrega, de forma preguiçosa.
# Só compõe as funções recebidas. Ex.: pipeline(filmes, tem_nota_valida, metrica("nota"), media)
pipeline = lambda filmes, selecao, transformacao, agregacao: agregacao(
    filter(lambda v: v is not None, map(transformacao, filter(selecao, filmes))))

# Valida a base já lida: erro amigável (RuntimeError) em vez de traceback.
_validar_base = lambda caminho, ok_r: (
    _lancar(RuntimeError(
        f"{caminho} não é um banco válido de filmes ({ok_r[1]}). "
        f"Gere a base com: {COMANDO_INGESTAO}"))
    if not ok_r[0]
    else ok_r[1] if ok_r[1]
    else _lancar(RuntimeError(
        f"{caminho} não tem filmes com orçamento, faturamento, nota e ano válidos."))
)
# Lê o banco UMA vez (o iterador só dá uma volta e depende da conexão aberta) e devolve
# os filmes válidos como tupla imutável. A conexão é fechada antes de validar o resultado.
carregar_filmes = lambda caminho: (
    _lancar(RuntimeError(f"banco não encontrado: {caminho}. Gere a base antes com: {COMANDO_INGESTAO}"))
    if not Path(caminho).is_file()
    else (lambda conn: (
        lambda ok_r: (conn.close(), _validar_base(caminho, ok_r))[-1]
    )(_tentar(lambda: tuple(limpar(ler_filmes(conn))), sqlite3.DatabaseError)))(conectar(caminho))
)

# (valor, erro): vazio -> (None, None) = critério não informado.
_ler_ano = lambda texto: (
    (None, None) if not texto.strip()
    else (lambda ok_n: (ok_n[1], None) if ok_n[0]
          else (None, f"'{texto.strip()}' não é um ano válido (ex.: 1990)")
          )(_tentar(lambda: int(texto.strip()), ValueError))
)
_checar_nota = lambda valor, limpo: (
    (valor, None) if 0 <= valor <= 10
    else (None, f"a nota mínima deve estar entre 0 e 10 (recebi '{limpo}')")
)
# Aceita vírgula decimal e exige 0 <= nota <= 10.
_ler_nota = lambda texto: (
    (None, None) if not texto.strip()
    else (lambda ok_v: _checar_nota(ok_v[1], texto.strip()) if ok_v[0]
          else (None, f"'{texto.strip()}' não é um número (ex.: 7,5)")
          )(_tentar(lambda: float(texto.strip().replace(",", ".")), ValueError))
)

_listar_filtrados = lambda filmes, criterios: (
    lambda selecionados: (
        lambda maiores: (
            f"{len(selecionados)} filme(s) encontrado(s); "
            f"exibindo os {len(maiores)} de maior faturamento.\n{formatar_filmes(maiores)}")
    )(top_filmes(LIMITE_LISTAGEM, metrica("faturamento"), selecionados))
)(tuple(filtrar(make_filtro(**criterios), filmes)))

_filtrar_com = lambda filmes, genero, ano_e, nota_e: (
    lambda erros: (
        "Entrada inválida: " + "; ".join(erros) + ". Nada foi filtrado." if erros
        else _listar_filtrados(filmes, dict(filter(
            lambda par: par[1] is not None and par[1] != "",
            (("genero", genero), ("a_partir_de", ano_e[0]), ("nota_min", nota_e[0]))))))
    )(tuple(filter(None, (ano_e[1], nota_e[1]))))

# Os argumentos são avaliados em ordem: as três perguntas saem na sequência certa.
_acao_filtrar = lambda filmes, perguntar: (
    lambda genero, ano_e, nota_e: _filtrar_com(filmes, genero.strip(), ano_e, nota_e)
)(perguntar("Gênero (Enter para qualquer um): "),
  _ler_ano(perguntar("A partir da década/ano, ex.: 1990 (Enter para qualquer): ")),
  _ler_nota(perguntar("Nota mínima de 0 a 10, ex.: 7,5 (Enter para qualquer): ")))

_mediana = lambda filmes, nome: pipeline(
    filmes, tem_financeiro_valido, metrica(nome), mediana)
_acao_indicadores = lambda filmes: "\n".join((
    formatar_comparacao("Indicadores gerais", (("Todos os filmes", resumir(filmes)),)),
    f"Orçamento mediano:   {formatar_moeda(_mediana(filmes, 'orcamento') or 0)}",
    f"Faturamento mediano: {formatar_moeda(_mediana(filmes, 'faturamento') or 0)}",
))
_acao_generos = lambda filmes: (
    lambda grupos: formatar_comparacao(
        "Comparação por gênero (maior ROI primeiro)", top_grupos(len(grupos), "roi", grupos))
)(comparar_por_genero(filmes))
_acao_decadas = lambda filmes: formatar_comparacao(
    "Comparação por década",
    map(lambda par: (f"{par[0]}s", par[1]), comparar_por_decada(filmes).items()))
_acao_correlacao = lambda filmes: "\n\n".join((
    formatar_correlacao("Orçamento × faturamento", orcamento_vs_faturamento(filmes)),
    formatar_correlacao("Orçamento × nota", orcamento_vs_nota(filmes)),
))

_top_filmes_por = lambda nome, titulo, filmes: (
    f"{titulo}\n{formatar_filmes(top_filmes(TOP_N, metrica(nome), filmes))}")
_top_grupos_por = lambda dimensao_, indicador, titulo, rotulo, filmes: formatar_comparacao(
    titulo, map(lambda par: (rotulo(par[0]), par[1]),
                top_grupos(TOP_N, indicador, comparar_por(dimensao(dimensao_), filmes))))
RANKINGS = MappingProxyType({
    "1": ("Filmes com maior faturamento",
          partial(_top_filmes_por, "faturamento", f"Top {TOP_N} filmes por faturamento")),
    "2": ("Filmes mais bem avaliados",
          partial(_top_filmes_por, "nota", f"Top {TOP_N} filmes por nota")),
    "3": ("Filmes com maior ROI",
          partial(_top_filmes_por, "roi", f"Top {TOP_N} filmes por ROI")),
    "4": ("Gêneros com maior ROI médio",
          partial(_top_grupos_por, "genero", "roi", "Gêneros por ROI médio", str)),
    "5": ("Décadas com maior nota média",
          partial(_top_grupos_por, "decada", "nota", "Décadas por nota média", "{}s".format)),
})
_OPCOES_RANKING = "\n".join(map(lambda par: f"  {par[0]} - {par[1][0]}", RANKINGS.items()))
_acao_rankings = lambda filmes, perguntar: (
    lambda escolha: (
        RANKINGS[escolha][1](filmes) if escolha in RANKINGS
        else f"Ranking '{escolha}' não existe. Escolha de 1 a {len(RANKINGS)}.")
)(perguntar(f"Rankings disponíveis:\n{_OPCOES_RANKING}\nQual? ").strip())

_sem_perguntas = lambda funcao: lambda filmes, _perguntar: funcao(filmes)
ACOES = MappingProxyType({
    "1": ("Filtrar filmes", _acao_filtrar),
    "2": ("Indicadores gerais", _sem_perguntas(_acao_indicadores)),
    "3": ("Comparar gêneros", _sem_perguntas(_acao_generos)),
    "4": ("Comparar décadas", _sem_perguntas(_acao_decadas)),
    "5": ("Correlação orçamento × faturamento", _sem_perguntas(_acao_correlacao)),
    "6": ("Rankings", _acao_rankings),
})
texto_menu = lambda: "\n".join((
    "", "MENU", *map(lambda par: f"  {par[0]} - {par[1][0]}", ACOES.items()),
    "  0 - Sair", "Escolha uma opção: "))
# Roda a opção e devolve o texto; opção inexistente vira mensagem, não exceção.
executar = lambda opcao, filmes, perguntar: (
    f"Opção '{opcao.strip()}' não existe. Digite um número de 0 a {len(ACOES)}."
    if opcao.strip() not in ACOES
    else ACOES[opcao.strip()][1](filmes, perguntar))

# `input` que trata Ctrl-D (fim da entrada) como 'sair'.
_ler = lambda prompt: (
    lambda ok_v: ok_v[1] if ok_v[0] else "0")(_tentar(lambda: input(prompt), EOFError))

# O "laço" do menu, sem `for`/`while`: pede a escolha (count() = infinito) até uma
# saída (takewhile) e executa cada uma; map é preguiçoso, então os passos se alternam.
_laco = lambda filmes, ler, escrever: tuple(map(
    lambda escolha: escrever(executar(escolha, filmes, ler)),
    takewhile(lambda e: e.strip().lower() not in SAIDAS,
              map(lambda _: ler(texto_menu()), count()))))

_analisador = lambda: (
    lambda p: (
        p.add_argument("--banco", default=str(DB_PADRAO),
                       help=f"caminho do banco (padrão: {DB_PADRAO})"),
        p.add_argument("--ingerir", action="store_true",
                       help="baixa filmes do TMDB e grava no banco (precisa de TMDB_TOKEN)"),
        p.add_argument("--limite", type=int, default=LIMITE_PADRAO,
                       help=f"com --ingerir: quantos filmes buscar (padrão: {LIMITE_PADRAO})"),
        p,
    )[-1]
)(argparse.ArgumentParser(description="Análise de desempenho cinematográfico (menu interativo)."))

_menu = lambda argumentos, ler, escrever: (
    lambda filmes: (
        escrever(f"Base carregada: {len(filmes)} filmes com dados válidos."),
        _laco(filmes, ler, escrever),
        escrever("Até logo!"),
        0,
    )[-1]
)(carregar_filmes(argumentos.banco))
_comando = lambda argumentos, ler, escrever: (
    (ingerir(paginas_para(argumentos.limite), argumentos.banco), 0)[-1] if argumentos.ingerir
    else _menu(argumentos, ler, escrever))
_tratar_erro = lambda argumentos, erro, escrever: (
    (escrever("ingestão interrompida; o que já foi gravado está no banco."
              if argumentos.ingerir else "\nEncerrado."), 130)[-1]
    if isinstance(erro, KeyboardInterrupt)
    else (escrever(f"erro: {erro}"), 1)[-1])

# Interpreta os argumentos, executa e devolve o código de saída (0 = sucesso).
main = lambda argv=None, *, ler=_ler, escrever=exibir: (
    lambda argumentos: (
        lambda ok_r: ok_r[1] if ok_r[0] else _tratar_erro(argumentos, ok_r[1], escrever)
    )(_tentar(lambda: _comando(argumentos, ler, escrever), RuntimeError, KeyboardInterrupt))
)(_analisador().parse_args(argv))

# Ponto de entrada sem `if`: a expressão só executa quando o arquivo é rodado direto.
# (O `reconfigure` garante acentos no terminal do Windows.)
(__name__ == "__main__") and (
    hasattr(sys.stdout, "reconfigure") and sys.stdout.reconfigure(encoding="utf-8", errors="replace"),
    sys.exit(main()),
)
