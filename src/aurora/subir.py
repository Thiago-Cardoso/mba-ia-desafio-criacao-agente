"""Comando de subida da API (uvicorn com um único worker).

`montar_app` faz a composição completa (esquema, dados de referência,
reconciliação de confirmações, runtime ADK, rotas HTTP) sem subir o
servidor — é usada tanto por `main()` (produção, modelo Gemini real por
nome) quanto pelos testes de reinício real, que precisam da MESMA
composição de produção, só trocando o modelo pelo roteirizado de teste.
"""

from __future__ import annotations

import logging
from pathlib import Path

import uvicorn
from google.adk.models.base_llm import BaseLlm

from . import api, confirmacao, dados_iniciais, persistencia, runtime
from .config import ChaveGeminiAusenteError, caminhos_padrao, carregar_variaveis_de_ambiente, exigir_chave_gemini
from .trava import TravaOcupadaError, trava_exclusiva

_log = logging.getLogger(__name__)

MODELO_PADRAO = "gemini-3.5-flash-lite"  # candidato aprovado para o modelo real.


def calcular_codigo_inicial(dir_dados: str) -> int:
    """Primeiro número de código ainda não usado em `dados/reservas.json`:
    compartilhado entre `subir` e `restaurar`, para que os dois
    concordem sobre o valor inicial do contador durável mesmo que
    `restaurar` rode antes de qualquer `subir` (clone limpo, sem ordem
    obrigatória entre os dois comandos)."""
    reservas_iniciais = dados_iniciais.carregar_reservas_iniciais(dir_dados)
    return dados_iniciais.maior_numero_de_codigo(reservas_iniciais) + 1


def montar_app(
    *,
    dir_dados: str,
    caminho_app_db: str,
    caminho_adk_db: str,
    modelo: BaseLlm | str,
    modelo_reservas: BaseLlm | str | None = None,
    modelo_visitantes: BaseLlm | str | None = None,
    modelo_regulamento: BaseLlm | str | None = None,
):
    """Monta a aplicação completa, sem subir o servidor HTTP e sem a
    trava de exclusividade (quem chama decide o escopo da trava —
    `main()` cobre a subida inteira; os testes de reinício real cobrem
    só o processo que estão testando)."""
    Path(caminho_app_db).parent.mkdir(parents=True, exist_ok=True)
    Path(caminho_adk_db).parent.mkdir(parents=True, exist_ok=True)
    conexao = persistencia.conectar(caminho_app_db)

    persistencia.inicializar_esquema(conexao, codigo_inicial=calcular_codigo_inicial(dir_dados))
    persistencia.carregar_apartamentos_e_areas(
        conexao,
        apartamentos=dados_iniciais.carregar_apartamentos(dir_dados),
        areas=dados_iniciais.carregar_areas(dir_dados),
    )

    interrompidas = confirmacao.reconciliar_aprovadas_sem_execucao(conexao)
    if interrompidas:
        _log.warning(
            "subida: %d confirmação(ões) 'aprovada' sem execução marcada(s) 'interrompida': %s",
            len(interrompidas),
            interrompidas,
        )

    rt = runtime.construir_runtime(
        conexao_negocio=conexao,
        caminho_sessao_adk=caminho_adk_db,
        caminho_regulamento=f"{dir_dados}/regulamento.md",
        modelo=modelo,
        modelo_reservas=modelo_reservas,
        modelo_visitantes=modelo_visitantes,
        modelo_regulamento=modelo_regulamento,
    )
    app = api.criar_app(conexao_negocio=conexao, runtime_adk=rt)
    return conexao, rt, app


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    carregar_variaveis_de_ambiente()
    try:
        exigir_chave_gemini()
    except ChaveGeminiAusenteError as exc:
        _log.error("não foi possível subir: %s", exc)
        raise SystemExit(1) from exc

    caminhos = caminhos_padrao()

    try:
        with trava_exclusiva(caminhos["caminho_lock"]):
            _, _, app = montar_app(
                dir_dados=caminhos["dir_dados"],
                caminho_app_db=caminhos["caminho_app_db"],
                caminho_adk_db=caminhos["caminho_adk_db"],
                modelo=MODELO_PADRAO,
            )
            # Um único worker: `uvicorn.run` recebendo uma instância de
            # `app` (não uma string de importação) nunca cria múltiplos
            # processos de worker — o parâmetro `workers` só existe no
            # modo de importação por string, por isso nem é passado aqui.
            uvicorn.run(app, host="127.0.0.1", port=8000)
    except TravaOcupadaError as exc:
        _log.error("não foi possível subir: %s", exc)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
