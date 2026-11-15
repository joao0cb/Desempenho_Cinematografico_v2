"""Régua das regras da professora, aplicada ao main.py por AST.

Regras:
  1. nada de `def` (nem async);
  2. nada de `for` / `while` como instrução;
  3. nada de `if` como instrução;
  4. compreensões (que contêm `for`) e `a if c else b` só DENTRO de lambda;
  5. tudo no main.py.

Além disso, o arquivo é mantido na leitura mais estrita: sem `try`, `with`, `raise`,
`class`, `global`, `assert`, e sem truques de `exec`/`eval`/`compile` (que
esconderiam um `def` dentro de texto). No nível do módulo só entram `import`,
atribuições e expressões.
"""
import ast
from pathlib import Path

import pytest

ARQUIVO = Path(__file__).resolve().parent.parent / "main.py"

PROIBIDOS = {
    ast.FunctionDef: "def", ast.AsyncFunctionDef: "async def",
    ast.For: "for (instrução)", ast.AsyncFor: "async for", ast.While: "while",
    ast.If: "if (instrução)", ast.Try: "try", ast.With: "with", ast.AsyncWith: "async with",
    ast.Raise: "raise", ast.ClassDef: "class", ast.Global: "global",
    ast.Nonlocal: "nonlocal", ast.Assert: "assert", ast.Delete: "del",
    ast.Return: "return", ast.Yield: "yield", ast.YieldFrom: "yield from",
    ast.Match: "match",
}
SO_EM_LAMBDA = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp, ast.IfExp)
CHAMADAS_PROIBIDAS = {"exec", "eval", "compile", "__import__"}
STATEMENTS_DO_MODULO = (ast.Import, ast.ImportFrom, ast.Assign, ast.Expr)


def _violacoes(no, dentro_de_lambda=False):
    achados = []
    if type(no) in PROIBIDOS:
        achados.append((no.lineno, PROIBIDOS[type(no)]))
    if isinstance(no, SO_EM_LAMBDA) and not dentro_de_lambda:
        achados.append((no.lineno, f"{type(no).__name__} fora de lambda"))
    if isinstance(no, ast.Call) and isinstance(no.func, ast.Name) and no.func.id in CHAMADAS_PROIBIDAS:
        achados.append((no.lineno, f"chamada a {no.func.id}()"))
    dentro = dentro_de_lambda or isinstance(no, ast.Lambda)
    for filho in ast.iter_child_nodes(no):
        achados.extend(_violacoes(filho, dentro))
    return achados


@pytest.fixture(scope="module")
def arvore():
    return ast.parse(ARQUIVO.read_text(encoding="utf-8"))


def test_o_programa_inteiro_esta_no_main_py():
    assert ARQUIVO.is_file()


def test_nenhuma_regra_e_quebrada(arvore):
    achados = sorted(set(_violacoes(arvore)))
    assert achados == [], "\n".join(f"linha {n}: {o}" for n, o in achados)


def test_no_nivel_do_modulo_so_ha_import_atribuicao_e_expressao(arvore):
    fora = [(no.lineno, type(no).__name__) for no in arvore.body
            if not isinstance(no, STATEMENTS_DO_MODULO)]
    assert fora == []


def test_ha_lambdas_de_verdade(arvore):
    assert sum(isinstance(no, ast.Lambda) for no in ast.walk(arvore)) > 100


def test_a_regua_pega_cada_violacao():
    """A própria régua precisa funcionar: cada trecho abaixo deve ser reprovado."""
    ruins = {
        "def": "def f(x):\n    return x",
        "for": "for x in range(3):\n    pass",
        "while": "while True:\n    pass",
        "if": "if True:\n    pass",
        "compreensão solta": "xs = [x for x in range(3)]",
        "ifexp solto": "y = 1 if True else 2",
        "try": "try:\n    pass\nexcept Exception:\n    pass",
        "exec": "exec('def f(): pass')",
        "def dentro de lambda": "f = lambda: (lambda: 0)\ndef g():\n    pass",
    }
    for nome, codigo in ruins.items():
        assert _violacoes(ast.parse(codigo)), f"a régua não reprovou: {nome}"
    bom = "f = lambda xs: [x for x in xs if x] if xs else ()\nprint(f([1]))"
    assert _violacoes(ast.parse(bom)) == []
