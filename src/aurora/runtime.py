"""Fábrica do runtime ADK: `App`, `Runner` e `DatabaseSessionService`.

O arquivo de sessões do ADK (`caminho_sessao_adk`) é separado do banco de
negócio (`persistencia.py`): a trava de sessão do ADK não deve envolver
as escritas de negócio. Em produção, `caminho_sessao_adk` é
`data/adk.db` (comando `subir`); em teste, um arquivo temporário.

Esta fábrica **não** decide a versão do ADK, nem o serviço de sessão
alternativo (`SqliteSessionService`), nem se a resumabilidade é ligada —
usa sempre o `DatabaseSessionService`, sem resumabilidade.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from google.adk.apps import App
from google.adk.models.base_llm import BaseLlm
from google.adk.runners import Runner
from google.adk.sessions import BaseSessionService, DatabaseSessionService

from . import agentes

NOME_APP = "aurora"

# Não há autenticação: o apartamento enviado na criação da sessão
# representa o morador autenticado, e não há múltiplos usuários por
# apartamento. Por isso um único `user_id` fixo do ADK é suficiente;
# é usado tanto aqui quanto na API.
USUARIO_PADRAO = "default"


@dataclass
class Runtime:
    app: App
    runner: Runner
    session_service: BaseSessionService


def construir_runtime(
    *,
    conexao_negocio: sqlite3.Connection,
    caminho_sessao_adk: str,
    caminho_regulamento: str,
    modelo: BaseLlm | str,
    modelo_reservas: BaseLlm | str | None = None,
    modelo_visitantes: BaseLlm | str | None = None,
    modelo_regulamento: BaseLlm | str | None = None,
) -> Runtime:
    """Monta o agente principal com os especialistas, o `App` e o `Runner`
    com `DatabaseSessionService` sobre `caminho_sessao_adk`.

    `modelo`/`modelo_reservas`/`modelo_visitantes`/`modelo_regulamento`:
    ver `agentes.criar_agente_principal`. Nenhum modelo é escolhido por
    padrão: o chamador decide (teste com modelo falso, ou execução com o
    modelo real). `caminho_regulamento` aponta para
    `dados/regulamento.md`; não tem padrão porque o caminho real em
    produção é decidido pelo comando `subir`.
    """
    principal = agentes.criar_agente_principal(
        conexao_negocio,
        caminho_regulamento=caminho_regulamento,
        modelo=modelo,
        modelo_reservas=modelo_reservas,
        modelo_visitantes=modelo_visitantes,
        modelo_regulamento=modelo_regulamento,
    )
    app = App(name=NOME_APP, root_agent=principal)
    session_service = DatabaseSessionService(f"sqlite+aiosqlite:///{caminho_sessao_adk}")
    runner = Runner(app=app, session_service=session_service)
    return Runtime(app=app, runner=runner, session_service=session_service)


async def criar_sessao(runtime: Runtime, *, apartamento: str, user_id: str = USUARIO_PADRAO) -> str:
    """Cria uma sessão do ADK com o apartamento gravado no estado — a
    única vez em que o apartamento é definido. Devolve o `session_id`."""
    sessao = await runtime.session_service.create_session(
        app_name=runtime.app.name, user_id=user_id, state={"apartamento": apartamento}
    )
    return sessao.id
