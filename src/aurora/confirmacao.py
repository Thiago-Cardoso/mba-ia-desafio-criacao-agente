"""Serviço de confirmação: a ponte entre os eventos de confirmação do ADK
e o registro de confirmações, no nível de serviço (sem HTTP — rotas HTTP
ficam em `api.py`).

Verificado com o ADK 2.12.0 instalado: o `FunctionCall`
`adk_request_confirmation` tem `fc.args` como um `dict` real (não
string), no formato:

```python
{
    "originalFunctionCall": {"id": "...", "args": {...}, "name": "reservar_area"},
    "toolConfirmation": {"hint": "...", "confirmed": False},
}
```

`fc.id` é o id da própria chamada `adk_request_confirmation`
(`adk_confirmation_fc_id`); `originalFunctionCall.id` é o id da chamada
ORIGINAL da tool (`original_function_call_id`), o mesmo que
`tool_context.function_call_id` recebe quando a tool é reexecutada após
a aprovação.
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from typing import Any, Optional

from google.adk.events.event import Event
from google.adk.runners import Runner
from google.genai import types

from . import persistencia

NOME_FUNCAO_CONFIRMACAO = "adk_request_confirmation"


class ConfirmacaoInvalidaError(Exception):
    """Levantada quando a transição `pendente -> aprovada`/`negada` falha:
    `id` inexistente, de outra sessão, ou já respondido (reenvio).

    Nada é executado nem enviado ao ADK quando este erro é levantado — a
    rota HTTP deve traduzi-lo em `409`.
    """


@dataclass
class ResultadoResposta:
    """Resultado de `responder_confirmacao`: estado final da confirmação e
    os eventos que a retomada do ADK gerou (vazios em caso de negação sem
    reexecução de tool)."""

    id: str
    estado: str
    motivo: Optional[str]
    resultado: Optional[dict[str, Any]]
    eventos: list[Event]


def _detalhes_da_acao(nome_tool: str, args: dict) -> dict:
    """Extrai `area`/`data` ou `nome`/`data` dos argumentos da chamada
    original, para preencher `detalhes` da pendência."""
    if nome_tool == "reservar_area":
        return {"area": args.get("area"), "data": args.get("data")}
    if nome_tool == "autorizar_visitante":
        return {"nome": args.get("nome"), "data": args.get("data")}
    return {}


def sincronizar_pendencias(
    conexao_negocio: sqlite3.Connection,
    eventos: list[Event],
    *,
    session_id: str,
    apartamento: str,
) -> list[str]:
    """Observa uma lista de eventos do ADK (de uma resposta recém-recebida,
    ou recuperados de uma sessão persistida na reconciliação) e cria, no
    registro, uma linha `pendente` para cada `adk_request_confirmation`
    ainda não conhecida.

    **Idempotente** por `original_function_call_id` (índice único de uso,
    `ix_confirmacoes_original_fc`, mais a verificação explícita abaixo):
    chamar esta função duas vezes com os mesmos eventos — por exemplo, uma
    vez no fluxo normal e outra na reconciliação após um reinício — não
    duplica a pendência.

    Usada tanto para a criação normal de pendências (chamada depois de
    `enviar_mensagem`) quanto para a reconciliação de pendências perdidas
    (chamada no comando de subida, com os eventos lidos de volta do
    `DatabaseSessionService`, não da resposta em memória — isso é o que
    torna a reconciliação capaz de recuperar de uma queda do processo).

    Retorna os `id`s das confirmações novas criadas nesta chamada.
    """
    criadas: list[str] = []
    for ev in eventos:
        for fc in ev.get_function_calls():
            if fc.name != NOME_FUNCAO_CONFIRMACAO:
                continue
            original = fc.args.get("originalFunctionCall") or {}
            original_id = original.get("id")
            original_name = original.get("name")
            original_args = original.get("args") or {}
            if original_id is None:
                continue  # formato inesperado; nada a registrar com segurança.

            existente = persistencia.obter_confirmacao_por_function_call_id(
                conexao_negocio, original_function_call_id=original_id
            )
            if existente is not None:
                continue  # já observada (idempotência da reconciliação).

            novo_id = str(uuid.uuid4())
            detalhes = _detalhes_da_acao(original_name, original_args)
            with persistencia.transacao(conexao_negocio):
                persistencia.criar_confirmacao_pendente(
                    conexao_negocio,
                    id=novo_id,
                    session_id=session_id,
                    apartamento=apartamento,
                    acao=original_name,
                    original_function_call_id=original_id,
                    adk_confirmation_fc_id=fc.id,
                    **detalhes,
                )
            criadas.append(novo_id)
    return criadas


async def enviar_mensagem(
    conexao_negocio: sqlite3.Connection,
    runner: Runner,
    *,
    session_id: str,
    user_id: str,
    apartamento: str,
    texto: str,
) -> list[Event]:
    """Envia uma mensagem do morador ao Runner e sincroniza as pendências
    de confirmação observadas na resposta.

    Devolve a lista de eventos da resposta, para o chamador (a rota HTTP)
    montar `resposta` e `confirmacoes_pendentes`.
    """
    mensagem = types.Content(role="user", parts=[types.Part(text=texto)])
    eventos: list[Event] = []
    async for ev in runner.run_async(user_id=user_id, session_id=session_id, new_message=mensagem):
        eventos.append(ev)
    sincronizar_pendencias(conexao_negocio, eventos, session_id=session_id, apartamento=apartamento)
    return eventos


async def responder_confirmacao(
    conexao_negocio: sqlite3.Connection,
    runner: Runner,
    *,
    session_id: str,
    user_id: str,
    id: str,
    confirmado: bool,
) -> ResultadoResposta:
    """Responde a uma confirmação pendente: faz primeiro a transição
    atômica de estado e, só se ela der certo, envia a `FunctionResponse`
    ao ADK.

    A transição `pendente -> aprovada` (ou `-> negada`) é verificada
    ANTES de qualquer contato com o ADK. Se ela falhar — `id`
    inexistente, de outra sessão, já aprovada, já negada, ou já
    executada — levanta `ConfirmacaoInvalidaError` e **nada** é enviado
    ao ADK. É exatamente a proteção que falta no próprio ADK: testes
    locais observaram o ADK 2.2.0 executar a ação de novo quando a
    aprovação é reenviada no mesmo processo — aqui a decisão nunca chega
    ao ADK pela segunda vez.

    Quando `confirmado=False`, a transição é `pendente -> negada`
    (terminal); o ADK recebe a rejeição e resolve a chamada original sem
    jamais invocar o corpo da tool (verificado nos testes: nenhuma
    gravação ocorre em negação). Quando `confirmado=True`, a transição é
    `pendente -> aprovada`; o ADK reexecuta a tool, que então localiza a
    linha `aprovada` (por `tool_context.function_call_id`) e grava o
    efeito de negócio na mesma transação que marca `executada`
    (`tools.py`).
    """
    conf = persistencia.obter_confirmacao(conexao_negocio, id=id, session_id=session_id)
    if conf is None:
        raise ConfirmacaoInvalidaError(
            f"confirmação {id!r} não existe na sessão {session_id!r}"
        )

    destino = "aprovada" if confirmado else "negada"
    with persistencia.transacao(conexao_negocio):
        ok = persistencia.transicionar_confirmacao(
            conexao_negocio, id=id, session_id=session_id, origem="pendente", destino=destino
        )
    if not ok:
        raise ConfirmacaoInvalidaError(
            f"confirmação {id!r} não está pendente na sessão {session_id!r} "
            "(reenvio, já respondida ou id inválido)"
        )

    resposta_adk = types.Content(
        role="user",
        parts=[
            types.Part(
                function_response=types.FunctionResponse(
                    id=conf["adk_confirmation_fc_id"],
                    name=NOME_FUNCAO_CONFIRMACAO,
                    response={"confirmed": confirmado},
                )
            )
        ],
    )
    eventos: list[Event] = []
    try:
        async for ev in runner.run_async(user_id=user_id, session_id=session_id, new_message=resposta_adk):
            eventos.append(ev)
    except Exception:
        # Se a ação já foi gravada e a confirmação já foi marcada
        # 'executada' (dentro da mesma transação, em tools.py) ANTES da
        # exceção, a falha é posterior ao efeito durável — a aplicação
        # relê o registro, que já tem o resultado real, e devolve sucesso
        # em vez de propagar o erro. Isso nunca inventa um resultado: só
        # lê o que já estava gravado. Se a confirmação não chegou a
        # 'executada', o efeito nunca aconteceu, a falha é genuína, e a
        # exceção é relançada sem alteração — a rota HTTP traduz isso em
        # `500` genérico, e a subida seguinte reconcilia a linha
        # 'aprovada' para 'interrompida'.
        conf_apos_falha = persistencia.obter_confirmacao(conexao_negocio, id=id, session_id=session_id)
        if conf_apos_falha is None or conf_apos_falha["estado"] != "executada":
            raise
        return ResultadoResposta(
            id=id,
            estado=conf_apos_falha["estado"],
            motivo=conf_apos_falha["motivo"],
            resultado=conf_apos_falha["resultado"],
            eventos=eventos,
        )

    conf_final = persistencia.obter_confirmacao(conexao_negocio, id=id, session_id=session_id)
    return ResultadoResposta(
        id=id,
        estado=conf_final["estado"],
        motivo=conf_final["motivo"],
        resultado=conf_final["resultado"],
        eventos=eventos,
    )


def reconciliar_aprovadas_sem_execucao(conexao_negocio: sqlite3.Connection) -> list[str]:
    """Confirmações `aprovada` que não chegaram a `executada`, `falhou`
    nem `interrompida` são marcadas `interrompida`, sem reexecução
    automática. Chamada pelo comando de subida.

    Retorna os `id`s marcados `interrompida`.
    """
    marcadas: list[str] = []
    for conf in persistencia.listar_aprovadas_sem_execucao(conexao_negocio):
        with persistencia.transacao(conexao_negocio):
            ok = persistencia.transicionar_confirmacao(
                conexao_negocio,
                id=conf["id"],
                session_id=conf["session_id"],
                origem="aprovada",
                destino="interrompida",
            )
        if ok:
            marcadas.append(conf["id"])
    return marcadas
