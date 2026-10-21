"""Testes de integração do serviço de confirmação, no nível de serviço
(sem HTTP — rotas HTTP ficam em `api.py`), com um modelo falso
roteirizado, determinístico e sem rede.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from google.genai import types

from aurora import confirmacao, persistencia, runtime

from .modelo_falso import ModeloFalso

DADOS = Path(__file__).resolve().parents[2] / "dados"


@pytest.fixture
def conexao_negocio(tmp_path):
    caminho = str(tmp_path / "app.db")
    conn = persistencia.conectar(caminho)
    persistencia.inicializar_esquema(conn, codigo_inicial=4822)
    conn.execute("INSERT INTO areas (id, nome, taxa) VALUES ('quadra', 'Quadra poliesportiva', 0)")
    conn.execute(
        "INSERT INTO areas (id, nome, taxa) VALUES ('salao-de-festas', 'Salão de festas', 150.0)"
    )
    conn.commit()
    yield conn
    conn.close()


def _construir_runtime(
    conexao_negocio, tmp_path, *, principal, reservas=None, visitantes=None, regulamento=None
):
    return runtime.construir_runtime(
        conexao_negocio=conexao_negocio,
        caminho_sessao_adk=str(tmp_path / "adk.db"),
        caminho_regulamento=str(DADOS / "regulamento.md"),
        modelo=principal,
        modelo_reservas=reservas or ModeloFalso(),
        modelo_visitantes=visitantes or ModeloFalso(),
        modelo_regulamento=regulamento or ModeloFalso(),
    )


def _pedir_reserva_com_taxa(conexao_negocio, tmp_path, apartamento="101", area="salao-de-festas", data="2030-04-20"):
    """Monta o runtime, cria a sessão e pede uma reserva que exige
    confirmação. Devolve `(rt, session_id, id_da_pendencia)`."""
    principal = ModeloFalso(fila=[("transferir", "reservas")])
    reservas = ModeloFalso(fila=[("chamar", "reservar_area", {"area": area, "data": data})])
    rt = _construir_runtime(conexao_negocio, tmp_path, principal=principal, reservas=reservas)
    sid = asyncio.run(runtime.criar_sessao(rt, apartamento=apartamento))
    asyncio.run(
        confirmacao.enviar_mensagem(
            conexao_negocio,
            rt.runner,
            session_id=sid,
            user_id="default",
            apartamento=apartamento,
            texto=f"Reserve {area} para {data}.",
        )
    )
    pendentes = persistencia.listar_pendentes(conexao_negocio, sid)
    assert len(pendentes) == 1
    return rt, sid, pendentes[0]["id"]


def _pedir_autorizacao_visitante(conexao_negocio, tmp_path, apartamento="101", nome="Joana Ribeiro", data="2030-04-21"):
    principal = ModeloFalso(fila=[("transferir", "visitantes")])
    visitantes = ModeloFalso(fila=[("chamar", "autorizar_visitante", {"nome": nome, "data": data})])
    rt = _construir_runtime(conexao_negocio, tmp_path, principal=principal, visitantes=visitantes)
    sid = asyncio.run(runtime.criar_sessao(rt, apartamento=apartamento))
    asyncio.run(
        confirmacao.enviar_mensagem(
            conexao_negocio,
            rt.runner,
            session_id=sid,
            user_id="default",
            apartamento=apartamento,
            texto=f"Libera a entrada de {nome} no dia {data}.",
        )
    )
    pendentes = persistencia.listar_pendentes(conexao_negocio, sid)
    assert len(pendentes) == 1
    return rt, sid, pendentes[0]["id"]


# ---------------------------------------------------------------------------
# Criação e aprovação
# ---------------------------------------------------------------------------


def test_pedido_com_taxa_cria_confirmacao_com_area_e_data_e_nada_e_gravado_antes(conexao_negocio, tmp_path):
    _, sid, id_pendencia = _pedir_reserva_com_taxa(conexao_negocio, tmp_path)
    pendencia = persistencia.obter_confirmacao(conexao_negocio, id=id_pendencia, session_id=sid)
    assert pendencia["area"] == "salao-de-festas"
    assert pendencia["data"] == "2030-04-20"
    assert pendencia["estado"] == "pendente"
    assert persistencia.listar_reservas_ativas(conexao_negocio, "101") == []


def test_aprovacao_executa_a_reserva_exatamente_uma_vez(conexao_negocio, tmp_path):
    rt, sid, id_pendencia = _pedir_reserva_com_taxa(conexao_negocio, tmp_path)

    resultado = asyncio.run(
        confirmacao.responder_confirmacao(
            conexao_negocio, rt.runner, session_id=sid, user_id="default", id=id_pendencia, confirmado=True
        )
    )

    assert resultado.estado == "executada"
    assert resultado.resultado["reservado"] is True
    ativas = persistencia.listar_reservas_ativas(conexao_negocio, "101")
    assert len(ativas) == 1
    assert ativas[0]["area"] == "salao-de-festas"
    assert ativas[0]["data"] == "2030-04-20"


def test_visitante_cria_confirmacao_com_nome_e_data_e_so_grava_apos_aprovacao(conexao_negocio, tmp_path):
    rt, sid, id_pendencia = _pedir_autorizacao_visitante(conexao_negocio, tmp_path)
    pendencia = persistencia.obter_confirmacao(conexao_negocio, id=id_pendencia, session_id=sid)
    assert pendencia["nome"] == "Joana Ribeiro"
    assert pendencia["data"] == "2030-04-21"
    assert persistencia.listar_visitantes(conexao_negocio, "101") == []

    resultado = asyncio.run(
        confirmacao.responder_confirmacao(
            conexao_negocio, rt.runner, session_id=sid, user_id="default", id=id_pendencia, confirmado=True
        )
    )

    assert resultado.estado == "executada"
    assert persistencia.listar_visitantes(conexao_negocio, "101") == [
        {"nome": "Joana Ribeiro", "data": "2030-04-21"}
    ]


def test_reserva_sem_taxa_nao_cria_confirmacao(conexao_negocio, tmp_path):
    principal = ModeloFalso(fila=[("transferir", "reservas")])
    reservas = ModeloFalso(fila=[("chamar", "reservar_area", {"area": "quadra", "data": "2030-04-06"})])
    rt = _construir_runtime(conexao_negocio, tmp_path, principal=principal, reservas=reservas)
    sid = asyncio.run(runtime.criar_sessao(rt, apartamento="101"))

    asyncio.run(
        confirmacao.enviar_mensagem(
            conexao_negocio, rt.runner, session_id=sid, user_id="default", apartamento="101",
            texto="Reserve a quadra para 2030-04-06.",
        )
    )

    assert persistencia.listar_pendentes(conexao_negocio, sid) == []
    assert len(persistencia.listar_reservas_ativas(conexao_negocio, "101")) == 1


def test_pendencia_nao_e_criada_a_partir_do_texto_e_ja_confirmei_nao_grava(conexao_negocio, tmp_path):
    """Mesmo que o texto do morador diga "já confirmei", a
    pendência continua até a rota de confirmações ser chamada de verdade —
    o que aqui significa que `enviar_mensagem` nunca grava nada por conta
    própria, e só `responder_confirmacao` pode."""
    _, sid, id_pendencia = _pedir_reserva_com_taxa(conexao_negocio, tmp_path)

    # a pendência foi criada só pela chamada da tool (via adk_request_confirmation),
    # nunca por texto. "Já confirmei" nunca é processado por esta função:
    # nenhum caminho de código em `confirmacao.py` ou `tools.py` lê o texto
    # da conversa para decidir uma transição — só a rota de confirmações
    # (aqui, `responder_confirmacao`) pode mudar o estado.
    assert persistencia.obter_confirmacao(conexao_negocio, id=id_pendencia, session_id=sid)["estado"] == "pendente"
    assert persistencia.listar_reservas_ativas(conexao_negocio, "101") == []


# ---------------------------------------------------------------------------
# Negação
# ---------------------------------------------------------------------------


def test_negacao_nao_grava_nada(conexao_negocio, tmp_path):
    rt, sid, id_pendencia = _pedir_reserva_com_taxa(conexao_negocio, tmp_path)

    resultado = asyncio.run(
        confirmacao.responder_confirmacao(
            conexao_negocio, rt.runner, session_id=sid, user_id="default", id=id_pendencia, confirmado=False
        )
    )

    assert resultado.estado == "negada"
    assert persistencia.listar_reservas_ativas(conexao_negocio, "101") == []


def test_aprovar_depois_de_negar_nao_executa(conexao_negocio, tmp_path):
    """Negação e depois aprovação do mesmo `id` não executa nada."""
    rt, sid, id_pendencia = _pedir_reserva_com_taxa(conexao_negocio, tmp_path)
    asyncio.run(
        confirmacao.responder_confirmacao(
            conexao_negocio, rt.runner, session_id=sid, user_id="default", id=id_pendencia, confirmado=False
        )
    )

    with pytest.raises(confirmacao.ConfirmacaoInvalidaError):
        asyncio.run(
            confirmacao.responder_confirmacao(
                conexao_negocio, rt.runner, session_id=sid, user_id="default", id=id_pendencia, confirmado=True
            )
        )

    assert persistencia.obter_confirmacao(conexao_negocio, id=id_pendencia, session_id=sid)["estado"] == "negada"
    assert persistencia.listar_reservas_ativas(conexao_negocio, "101") == []


# ---------------------------------------------------------------------------
# Reenvio
# ---------------------------------------------------------------------------


def test_reenvio_de_aprovacao_ja_respondida(conexao_negocio, tmp_path):
    """Reenviar a mesma aprovação não cria nova linha nem executa de
    novo, e nada é enviado ao ADK na segunda vez (a exceção é levantada
    ANTES de montar a `FunctionResponse` — ver
    `confirmacao.responder_confirmacao`)."""
    rt, sid, id_pendencia = _pedir_reserva_com_taxa(conexao_negocio, tmp_path)

    primeiro = asyncio.run(
        confirmacao.responder_confirmacao(
            conexao_negocio, rt.runner, session_id=sid, user_id="default", id=id_pendencia, confirmado=True
        )
    )
    assert primeiro.estado == "executada"
    assert len(persistencia.listar_reservas_ativas(conexao_negocio, "101")) == 1

    with pytest.raises(confirmacao.ConfirmacaoInvalidaError):
        asyncio.run(
            confirmacao.responder_confirmacao(
                conexao_negocio, rt.runner, session_id=sid, user_id="default", id=id_pendencia, confirmado=True
            )
        )

    # nenhuma segunda reserva foi criada.
    assert len(persistencia.listar_reservas_ativas(conexao_negocio, "101")) == 1


def test_reenvio_de_id_de_outra_sessao_nao_executa(conexao_negocio, tmp_path):
    """Responder numa sessão B o `id` de uma confirmação pendente da
    sessão A não executa nada, e a confirmação continua pendente em A."""
    rt, sid_a, id_pendencia = _pedir_reserva_com_taxa(conexao_negocio, tmp_path, apartamento="101")

    with pytest.raises(confirmacao.ConfirmacaoInvalidaError):
        asyncio.run(
            confirmacao.responder_confirmacao(
                conexao_negocio, rt.runner, session_id="sessao-de-outro-apartamento",
                user_id="default", id=id_pendencia, confirmado=True,
            )
        )

    assert persistencia.obter_confirmacao(conexao_negocio, id=id_pendencia, session_id=sid_a)["estado"] == "pendente"
    assert persistencia.listar_reservas_ativas(conexao_negocio, "101") == []


def test_id_inexistente_nao_executa(conexao_negocio, tmp_path):
    rt, sid, _ = _pedir_reserva_com_taxa(conexao_negocio, tmp_path)
    with pytest.raises(confirmacao.ConfirmacaoInvalidaError):
        asyncio.run(
            confirmacao.responder_confirmacao(
                conexao_negocio, rt.runner, session_id=sid, user_id="default",
                id="id-inexistente", confirmado=True,
            )
        )
    assert persistencia.listar_reservas_ativas(conexao_negocio, "101") == []


# ---------------------------------------------------------------------------
# Duas pendências na mesma sessão
# ---------------------------------------------------------------------------


def test_duas_pendencias_na_mesma_sessao_cada_uma_so_pelo_proprio_id(conexao_negocio, tmp_path):
    """Duas mensagens sequenciais na mesma sessão, cada uma pedindo uma
    reserva com taxa numa data diferente. **Comportamento observado do
    ADK 2.12.0, registrado aqui**: depois que `reservas` assume o
    controle por transferência, uma nova mensagem de texto do morador
    (não uma `FunctionResponse`) continua sendo roteada diretamente para
    `reservas`, sem voltar a passar pelo `principal` — por isso o
    roteiro da segunda mensagem é enfileirado direto no modelo falso de
    `reservas`, não do `principal`. O enunciado trata esse caso como
    comportamento livre: "nova mensagem enviada enquanto existe
    confirmação pendente"."""
    principal = ModeloFalso(fila=[("transferir", "reservas")])
    reservas = ModeloFalso(
        fila=[
            ("chamar", "reservar_area", {"area": "salao-de-festas", "data": "2030-04-20"}),
            ("chamar", "reservar_area", {"area": "salao-de-festas", "data": "2030-05-11"}),
        ]
    )
    rt = _construir_runtime(conexao_negocio, tmp_path, principal=principal, reservas=reservas)
    sid = asyncio.run(runtime.criar_sessao(rt, apartamento="101"))

    asyncio.run(
        confirmacao.enviar_mensagem(
            conexao_negocio, rt.runner, session_id=sid, user_id="default", apartamento="101",
            texto="Reserve o salão de festas para 2030-04-20.",
        )
    )
    asyncio.run(
        confirmacao.enviar_mensagem(
            conexao_negocio, rt.runner, session_id=sid, user_id="default", apartamento="101",
            texto="Reserve também o salão de festas para 2030-05-11.",
        )
    )

    pendentes = persistencia.listar_pendentes(conexao_negocio, sid)
    assert len(pendentes) == 2
    id_1 = next(p["id"] for p in pendentes if p["data"] == "2030-04-20")
    id_2 = next(p["id"] for p in pendentes if p["data"] == "2030-05-11")

    # aprovar a primeira não afeta a segunda.
    asyncio.run(
        confirmacao.responder_confirmacao(
            conexao_negocio, rt.runner, session_id=sid, user_id="default", id=id_1, confirmado=True
        )
    )
    assert [p["id"] for p in persistencia.listar_pendentes(conexao_negocio, sid)] == [id_2]
    assert len(persistencia.listar_reservas_ativas(conexao_negocio, "101")) == 1

    # aprovar a segunda agora resolve a pendência restante.
    asyncio.run(
        confirmacao.responder_confirmacao(
            conexao_negocio, rt.runner, session_id=sid, user_id="default", id=id_2, confirmado=True
        )
    )
    assert persistencia.listar_pendentes(conexao_negocio, sid) == []
    ativas = persistencia.listar_reservas_ativas(conexao_negocio, "101")
    assert sorted(a["data"] for a in ativas) == ["2030-04-20", "2030-05-11"]


# ---------------------------------------------------------------------------
# Data ocupada na execução
# ---------------------------------------------------------------------------


def test_data_ocupada_na_execucao_resulta_em_falhou_sem_erro_de_servidor(conexao_negocio, tmp_path):
    """Duas sessões pedem a mesma área/data; a primeira aprovação executa,
    a segunda vira `falhou` com `motivo='data_ocupada'` — uma recusa
    normal, não uma exceção não tratada."""
    rt1, sid1, id1 = _pedir_reserva_com_taxa(conexao_negocio, tmp_path, apartamento="101")
    rt2, sid2, id2 = _pedir_reserva_com_taxa(conexao_negocio, tmp_path, apartamento="201")

    resultado1 = asyncio.run(
        confirmacao.responder_confirmacao(
            conexao_negocio, rt1.runner, session_id=sid1, user_id="default", id=id1, confirmado=True
        )
    )
    assert resultado1.estado == "executada"

    resultado2 = asyncio.run(
        confirmacao.responder_confirmacao(
            conexao_negocio, rt2.runner, session_id=sid2, user_id="default", id=id2, confirmado=True
        )
    )
    assert resultado2.estado == "falhou"
    assert resultado2.motivo == "data_ocupada"
    assert resultado2.resultado == {"status": "ok", "reservado": False, "motivo": "data_ocupada"}

    # exclusividade: só uma reserva ativa para a área/data, não duas.
    assert len(persistencia.listar_reservas_ativas(conexao_negocio, "101")) == 1
    assert persistencia.listar_reservas_ativas(conexao_negocio, "201") == []


def test_excecao_depois_de_executada_nao_marca_falhou(conexao_negocio, tmp_path):
    """Simula uma tentativa (equivocada) de marcar `falhou` depois que a
    execução já tinha avançado para `executada`: a transição é invalida
    pela máquina de estados (a origem exigida para `falhou` é
    `aprovada`, não `executada`) e não altera nada."""
    rt, sid, id_pendencia = _pedir_reserva_com_taxa(conexao_negocio, tmp_path)
    resultado = asyncio.run(
        confirmacao.responder_confirmacao(
            conexao_negocio, rt.runner, session_id=sid, user_id="default", id=id_pendencia, confirmado=True
        )
    )
    assert resultado.estado == "executada"

    with persistencia.transacao(conexao_negocio):
        tentativa = persistencia.transicionar_confirmacao(
            conexao_negocio, id=id_pendencia, session_id=sid, origem="aprovada", destino="falhou",
            motivo="tentativa_tardia",
        )
    assert tentativa is False
    conf_final = persistencia.obter_confirmacao(conexao_negocio, id=id_pendencia, session_id=sid)
    assert conf_final["estado"] == "executada"
    assert conf_final["motivo"] is None  # a tentativa tardia não escreveu nada.


# ---------------------------------------------------------------------------
# Idempotência da reexecução "at least once"
# ---------------------------------------------------------------------------


def test_reexecutar_a_tool_apos_executada_nao_grava_de_novo(conexao_negocio, tmp_path):
    """Simula o ADK reexecutando a tool depois de já ter marcado
    `executada` (comportamento documentado como "at least once"). A tool
    detecta o estado `executada` e devolve o resultado já salvo, sem
    inserir uma segunda reserva."""
    from aurora import tools as tools_mod

    rt, sid, id_pendencia = _pedir_reserva_com_taxa(conexao_negocio, tmp_path)
    asyncio.run(
        confirmacao.responder_confirmacao(
            conexao_negocio, rt.runner, session_id=sid, user_id="default", id=id_pendencia, confirmado=True
        )
    )
    conf = persistencia.obter_confirmacao(conexao_negocio, id=id_pendencia, session_id=sid)
    assert conf["estado"] == "executada"

    class _ContextoReexecucao:
        def __init__(self, apartamento, function_call_id):
            self.state = {"apartamento": apartamento}
            self.function_call_id = function_call_id

    tool = next(
        t for t in tools_mod.criar_tools_reservas(conexao_negocio) if t.name == "reservar_area"
    )
    resultado_repetido = tool.func(
        area="salao-de-festas",
        data="2030-04-20",
        tool_context=_ContextoReexecucao("101", conf["original_function_call_id"]),
    )

    assert resultado_repetido == conf["resultado"]
    assert len(persistencia.listar_reservas_ativas(conexao_negocio, "101")) == 1


# ---------------------------------------------------------------------------
# Reconciliação de pendências perdidas
# ---------------------------------------------------------------------------


def test_reconciliacao_recupera_evento_sem_linha(conexao_negocio, tmp_path):
    """Simula uma pendência "perdida" — o ADK emitiu
    `adk_request_confirmation`, mas a aplicação, por hipótese, caiu
    antes de observar o evento e criar a linha `pendente`. A
    reconciliação lê os eventos
    **persistidos** da sessão (via `DatabaseSessionService.get_session`,
    não da resposta em memória) e recria a pendência, que passa a ser
    aprovável normalmente — e chamar a reconciliação de novo não duplica
    nada (idempotência)."""
    principal = ModeloFalso(fila=[("transferir", "reservas")])
    reservas = ModeloFalso(fila=[("chamar", "reservar_area", {"area": "salao-de-festas", "data": "2030-04-20"})])
    rt = _construir_runtime(conexao_negocio, tmp_path, principal=principal, reservas=reservas)
    sid = asyncio.run(runtime.criar_sessao(rt, apartamento="101"))

    # envia a mensagem SEM chamar confirmacao.enviar_mensagem (que
    # sincronizaria a pendência) — simula a aplicação "perdendo" o evento.
    async def _enviar_sem_sincronizar():
        msg = types.Content(role="user", parts=[types.Part(text="Reserve o salão de festas para 2030-04-20.")])
        eventos = []
        async for ev in rt.runner.run_async(user_id="default", session_id=sid, new_message=msg):
            eventos.append(ev)
        return eventos

    asyncio.run(_enviar_sem_sincronizar())
    assert persistencia.listar_pendentes(conexao_negocio, sid) == []  # "perdida": nada registrado ainda.

    # reconciliação: lê os eventos PERSISTIDOS da sessão (não a lista local).
    async def _reconciliar():
        sessao = await rt.session_service.get_session(app_name=rt.app.name, user_id="default", session_id=sid)
        return confirmacao.sincronizar_pendencias(
            conexao_negocio, sessao.events, session_id=sid, apartamento="101"
        )

    criadas = asyncio.run(_reconciliar())
    assert len(criadas) == 1

    pendentes = persistencia.listar_pendentes(conexao_negocio, sid)
    assert len(pendentes) == 1
    assert pendentes[0]["id"] == criadas[0]
    assert pendentes[0]["area"] == "salao-de-festas"

    # reconciliar de novo com os mesmos eventos não duplica (idempotência).
    criadas_de_novo = asyncio.run(_reconciliar())
    assert criadas_de_novo == []
    assert len(persistencia.listar_pendentes(conexao_negocio, sid)) == 1

    # a pendência recuperada é aprovável normalmente.
    resultado = asyncio.run(
        confirmacao.responder_confirmacao(
            conexao_negocio, rt.runner, session_id=sid, user_id="default",
            id=pendentes[0]["id"], confirmado=True,
        )
    )
    assert resultado.estado == "executada"
    assert len(persistencia.listar_reservas_ativas(conexao_negocio, "101")) == 1


# ---------------------------------------------------------------------------
# Reconciliação de "aprovada" sem execução
# ---------------------------------------------------------------------------


def test_reconciliar_aprovadas_sem_execucao_marca_interrompida(conexao_negocio, tmp_path):
    """Simula uma confirmação que avançou para `aprovada` mas nunca
    chegou a `executada` (por exemplo, queda do processo entre a
    aprovação e a execução da tool). A reconciliação marca
    `interrompida`, sem reexecução automática."""
    rt, sid, id_pendencia = _pedir_reserva_com_taxa(conexao_negocio, tmp_path)
    with persistencia.transacao(conexao_negocio):
        ok = persistencia.transicionar_confirmacao(
            conexao_negocio, id=id_pendencia, session_id=sid, origem="pendente", destino="aprovada"
        )
    assert ok is True

    marcadas = confirmacao.reconciliar_aprovadas_sem_execucao(conexao_negocio)

    assert marcadas == [id_pendencia]
    assert persistencia.obter_confirmacao(conexao_negocio, id=id_pendencia, session_id=sid)["estado"] == "interrompida"
    assert persistencia.listar_reservas_ativas(conexao_negocio, "101") == []


# ---------------------------------------------------------------------------
# Transições são chamadas com a origem correta (unitário, via o módulo real)
# ---------------------------------------------------------------------------


def test_transicoes_de_confirmacao_usam_a_origem_correta(conexao_negocio, tmp_path):
    rt, sid, id_pendencia = _pedir_reserva_com_taxa(conexao_negocio, tmp_path)

    # a transição para 'aprovada' só é aceita a partir de 'pendente'.
    with persistencia.transacao(conexao_negocio):
        assert persistencia.transicionar_confirmacao(
            conexao_negocio, id=id_pendencia, session_id=sid, origem="pendente", destino="aprovada"
        ) is True
    with persistencia.transacao(conexao_negocio):
        assert persistencia.transicionar_confirmacao(
            conexao_negocio, id=id_pendencia, session_id=sid, origem="pendente", destino="aprovada"
        ) is False  # já não está mais 'pendente'.
