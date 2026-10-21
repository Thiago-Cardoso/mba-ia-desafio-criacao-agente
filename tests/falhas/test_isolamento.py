"""Testes de isolamento da injeção de falhas: provam que o mecanismo de
falha vive só em `tests/falhas/` e que nada no código de produção pode
ativá-lo, de nenhuma forma.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

RAIZ_PILOTO = Path(__file__).resolve().parents[2]
SRC = RAIZ_PILOTO / "src" / "aurora"

TERMOS_DE_FALHA = [
    "AURORA_FALHA_MODO",
    "RunnerComFalhaInjetada",
    "envoltorio_runner",
    "falha_modo",
    "os._exit",
    "tests.falhas",
    "tests/falhas",
]


def _arquivos_fonte_de_producao() -> list[Path]:
    return sorted(SRC.glob("*.py"))


# ---------------------------------------------------------------------------
# O entrypoint de produção nunca importa o módulo de teste de falhas
# ---------------------------------------------------------------------------


def test_src_nao_importa_tests_falhas_estaticamente():
    """Busca estática: nenhum arquivo de `src/aurora/` contém a string
    `tests.falhas`, `tests/falhas` ou `envoltorio_runner`."""
    ocorrencias = []
    for arquivo in _arquivos_fonte_de_producao():
        texto = arquivo.read_text(encoding="utf-8")
        for termo in ("tests.falhas", "tests/falhas", "envoltorio_runner", "RunnerComFalhaInjetada"):
            if termo in texto:
                ocorrencias.append((arquivo.name, termo))
    assert ocorrencias == [], f"referência a módulo de teste de falhas encontrada em produção: {ocorrencias}"


def test_processo_de_producao_nao_carrega_modulo_de_falhas_em_tempo_de_execucao():
    """Sobe `aurora.subir` isoladamente (só importa, não chama `main()`,
    para não precisar de porta nem travar) e verifica, por um processo
    real, que `sys.modules` nunca contém nada de `tests.falhas`."""
    script = (
        "import sys; "
        "from aurora import subir, api, confirmacao, persistencia, tools, agentes, runtime, "
        "trava, restaurar, config, dados_iniciais, regulamento; "
        "suspeitos = [m for m in sys.modules if 'falhas' in m or 'envoltorio_runner' in m]; "
        "print('SUSPEITOS:' + repr(suspeitos))"
    )
    resultado = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(RAIZ_PILOTO),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert resultado.returncode == 0, resultado.stdout + resultado.stderr
    assert "SUSPEITOS:[]" in resultado.stdout, resultado.stdout


# ---------------------------------------------------------------------------
# Nenhuma rota, cabeçalho, parâmetro ou variável de produção ativa falha
# ---------------------------------------------------------------------------


def test_nenhum_arquivo_de_producao_le_variavel_de_falha():
    ocorrencias = []
    for arquivo in _arquivos_fonte_de_producao():
        texto = arquivo.read_text(encoding="utf-8")
        if "AURORA_FALHA_MODO" in texto or "falha_modo" in texto:
            ocorrencias.append(arquivo.name)
    assert ocorrencias == [], f"variável/parâmetro de falha referenciado em produção: {ocorrencias}"


def test_nenhuma_rota_da_api_de_producao_menciona_falha():
    """Inspeciona a lista real de rotas montada por `api.criar_app`
    (não uma lista escrita à mão) — nenhum caminho nem nome de rota
    contém "falha"/"fault"/"inject"."""
    import sqlite3
    import tempfile

    from aurora import api, persistencia, runtime

    with tempfile.TemporaryDirectory() as tmp:
        conexao = persistencia.conectar(f"{tmp}/app.db")
        persistencia.inicializar_esquema(conexao, codigo_inicial=1)
        rt = runtime.construir_runtime(
            conexao_negocio=conexao,
            caminho_sessao_adk=f"{tmp}/adk.db",
            caminho_regulamento=str(RAIZ_PILOTO / "dados" / "regulamento.md"),
            modelo="modelo-nao-usado-nesta-inspecao",
        )
        app = api.criar_app(conexao_negocio=conexao, runtime_adk=rt)
        caminhos = [getattr(rota, "path", "") for rota in app.routes]
        conexao.close()

    suspeitos = [
        c for c in caminhos if any(t in c.lower() for t in ("falha", "fault", "inject", "chaos"))
    ]
    assert suspeitos == [], f"rota suspeita de ativar falha: {suspeitos}"


# ---------------------------------------------------------------------------
# `src/` não contém nenhum ramo de código condicionado a falha injetada
# ---------------------------------------------------------------------------


def test_src_nao_contem_ramo_de_falha():
    """Busca estática por qualquer um dos termos de falha em `src/`.
    Esta é a verificação mais ampla: cobre nomes de variável, de classe
    de teste e a chamada `os._exit`, que não deve existir em nenhum
    arquivo de produção."""
    ocorrencias = []
    for arquivo in _arquivos_fonte_de_producao():
        texto = arquivo.read_text(encoding="utf-8")
        for termo in TERMOS_DE_FALHA:
            if termo in texto:
                ocorrencias.append((arquivo.name, termo))
    assert ocorrencias == [], f"ramo/termo de falha encontrado em produção: {ocorrencias}"
