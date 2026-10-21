"""API HTTP do contrato do enunciado.

As rotas integram o `runtime`, o `confirmacao` e a `persistencia` já
existentes — nenhuma lógica de estado de confirmação é duplicada aqui;
a rota só traduz exceções do serviço em códigos HTTP.

`criar_app` recebe um `Runtime` e uma conexão de negócio já construídos.
Decidir *quando* e *com que caminhos* construí-los (comando `subir`,
variáveis de ambiente, ciclo de vida do processo) é responsabilidade de
quem chama esta fábrica; ela não lê `.env` nem decide caminhos de
arquivo.
"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from google.adk.events.event import Event

from . import confirmacao, persistencia, runtime
from .confirmacao import ConfirmacaoInvalidaError
from .modelos_http import (
    ConfirmacaoEntrada,
    CriarSessaoEntrada,
    CriarSessaoSaida,
    MensagemEntrada,
    RespostaMensagem,
    ReservaSaida,
    VisitanteSaida,
)

_log = logging.getLogger(__name__)


def criar_app(*, conexao_negocio: sqlite3.Connection, runtime_adk: runtime.Runtime) -> FastAPI:
    app = FastAPI(title="Residencial Aurora")

    @app.exception_handler(Exception)
    async def _erro_nao_previsto(request: Request, exc: Exception) -> JSONResponse:
        """`500` genérico, sem rastreio de pilha, caminhos nem segredos no
        corpo. O detalhe vai só para o log do servidor, identificado por
        `id_requisicao`, para o operador correlacionar."""
        id_requisicao = str(uuid.uuid4())
        _log.exception("erro não tratado (id_requisicao=%s)", id_requisicao)
        return JSONResponse(
            status_code=500,
            content={"erro": "erro interno", "id_requisicao": id_requisicao},
        )

    async def _sessao_existe(session_id: str) -> dict | None:
        sessao = await runtime_adk.session_service.get_session(
            app_name=runtime_adk.app.name,
            user_id=runtime.USUARIO_PADRAO,
            session_id=session_id,
        )
        return sessao

    @app.post("/sessoes", status_code=201)
    async def criar_sessao_rota(corpo: CriarSessaoEntrada) -> CriarSessaoSaida:
        session_id = await runtime.criar_sessao(runtime_adk, apartamento=corpo.apartamento)
        return CriarSessaoSaida(session_id=session_id)

    @app.post("/sessoes/{session_id}/mensagens")
    async def enviar_mensagem_rota(session_id: str, corpo: MensagemEntrada) -> RespostaMensagem:
        sessao = await _sessao_existe(session_id)
        if sessao is None:
            raise HTTPException(status_code=404, detail="sessão inexistente")
        apartamento = sessao.state["apartamento"]
        eventos = await confirmacao.enviar_mensagem(
            conexao_negocio,
            runtime_adk.runner,
            session_id=session_id,
            user_id=runtime.USUARIO_PADRAO,
            apartamento=apartamento,
            texto=corpo.texto,
        )
        return _montar_resposta(conexao_negocio, session_id, eventos)

    @app.post("/sessoes/{session_id}/confirmacoes")
    async def responder_confirmacao_rota(session_id: str, corpo: ConfirmacaoEntrada) -> RespostaMensagem:
        sessao = await _sessao_existe(session_id)
        if sessao is None:
            raise HTTPException(status_code=404, detail="sessão inexistente")
        try:
            resultado = await confirmacao.responder_confirmacao(
                conexao_negocio,
                runtime_adk.runner,
                session_id=session_id,
                user_id=runtime.USUARIO_PADRAO,
                id=corpo.id,
                confirmado=corpo.confirmado,
            )
        except ConfirmacaoInvalidaError:
            raise HTTPException(
                status_code=409, detail="não existe confirmação pendente com esse id nesta sessão"
            )
        return _montar_resposta(conexao_negocio, session_id, resultado.eventos)

    @app.get("/sessoes/{session_id}/eventos")
    async def eventos_rota(session_id: str) -> list[dict[str, Any]]:
        sessao = await _sessao_existe(session_id)
        if sessao is None:
            raise HTTPException(status_code=404, detail="sessão inexistente")
        return [_evento_em_json(ev) for ev in sessao.events]

    @app.get("/apartamentos/{apartamento}/reservas")
    async def reservas_rota(apartamento: str) -> list[ReservaSaida]:
        # `async def` (não `def`): mantém a rota na mesma thread do loop de
        # eventos, junto das rotas de conversa — a conexão sqlite3 é usada
        # de uma só thread por vez, embora não necessariamente sempre a
        # mesma (ver `persistencia.conectar`, `check_same_thread=False`).
        return persistencia.listar_reservas_ativas(conexao_negocio, apartamento)

    @app.get("/apartamentos/{apartamento}/visitantes")
    async def visitantes_rota(apartamento: str) -> list[VisitanteSaida]:
        return persistencia.listar_visitantes(conexao_negocio, apartamento)

    return app


def _texto_resposta(eventos: list[Event]) -> str:
    """`resposta` do contrato: string vazia quando a execução parou
    esperando confirmação nesta chamada; senão, o último texto que o
    modelo produziu."""
    pendencia_nova = any(
        fc.name == confirmacao.NOME_FUNCAO_CONFIRMACAO for ev in eventos for fc in ev.get_function_calls()
    )
    if pendencia_nova:
        return ""
    textos = [
        parte.text
        for ev in eventos
        if ev.content and ev.content.parts
        for parte in ev.content.parts
        if parte.text
    ]
    return textos[-1] if textos else ""


def _detalhes_da_pendencia(pendente: dict) -> dict:
    if pendente["acao"] == "reservar_area":
        return {"area": pendente["area"], "data": pendente["data"]}
    if pendente["acao"] == "autorizar_visitante":
        return {"nome": pendente["nome"], "data": pendente["data"]}
    return {}


def _montar_resposta(
    conexao_negocio: sqlite3.Connection, session_id: str, eventos: list[Event]
) -> RespostaMensagem:
    """Monta `resposta`/`confirmacoes_pendentes` a partir do estado atual
    do registro, nunca da memória do modelo: mesmo numa resposta à rota
    de confirmações, a lista reflete TODAS as pendências da sessão no
    momento, não só a que acabou de ser respondida."""
    pendentes = persistencia.listar_pendentes(conexao_negocio, session_id)
    return RespostaMensagem(
        resposta=_texto_resposta(eventos),
        confirmacoes_pendentes=[
            {"id": p["id"], "acao": p["acao"], "detalhes": _detalhes_da_pendencia(p)}
            for p in pendentes
        ],
    )


def _evento_em_json(evento: Event) -> dict[str, Any]:
    """Conteúdo completo do evento, serializado em modo JSON do Pydantic
    (datas/bytes/enums na forma serializável)."""
    return evento.model_dump(mode="json", exclude_none=True)
