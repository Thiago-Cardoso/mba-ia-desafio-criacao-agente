"""Testes de integração da topologia ADK, com um modelo falso
roteirizado, determinístico e sem rede. Não provam escolha de tool com
o modelo real — isso é coberto pelos testes de fumaça com Gemini real
em `tests/smoke/`.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from google.genai import types

from aurora import agentes, persistencia, runtime, tools

from .modelo_falso import ModeloFalso

DADOS = Path(__file__).resolve().parents[2] / "dados"


class _ContextoFake:
    """Stub mínimo de `ToolContext` para chamar o corpo de uma tool
    diretamente (sem passar pelo Runner), quando o teste não precisa do
    roteamento do ADK — só da lógica da própria função."""

    def __init__(self, apartamento: str, *, function_call_id: str = "fc-inexistente"):
        self.state = {"apartamento": apartamento}
        self.function_call_id = function_call_id


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


async def _drain(runner, session_id, texto):
    eventos = []
    msg = types.Content(role="user", parts=[types.Part(text=texto)])
    async for ev in runner.run_async(user_id="default", session_id=session_id, new_message=msg):
        eventos.append(ev)
    return eventos


def _chamadas(eventos, nome=None):
    cs = [fc for e in eventos for fc in e.get_function_calls()]
    return [c for c in cs if nome is None or c.name == nome]


def _tool(lista, nome):
    return next(t for t in lista if t.name == nome)


# ---------------------------------------------------------------------------
# Transferência do principal para o especialista
# ---------------------------------------------------------------------------


def test_principal_transfere_para_reservas(conexao_negocio, tmp_path):
    principal = ModeloFalso(fila=[("transferir", "reservas")])
    reservas = ModeloFalso(
        fila=[("chamar", "verificar_disponibilidade", {"area": "quadra", "data": "2030-04-06"})]
    )
    rt = _construir_runtime(conexao_negocio, tmp_path, principal=principal, reservas=reservas)
    sid = asyncio.run(runtime.criar_sessao(rt, apartamento="101"))
    eventos = asyncio.run(_drain(rt.runner, sid, "A quadra está livre em 06/04/2030?"))

    assert "reservas" in [e.author for e in eventos]
    assert len(_chamadas(eventos, "verificar_disponibilidade")) == 1


def test_principal_transfere_para_visitantes(conexao_negocio, tmp_path):
    principal = ModeloFalso(fila=[("transferir", "visitantes")])
    visitantes = ModeloFalso(
        fila=[("chamar", "autorizar_visitante", {"nome": "Joana Ribeiro", "data": "2030-04-21"})]
    )
    rt = _construir_runtime(conexao_negocio, tmp_path, principal=principal, visitantes=visitantes)
    sid = asyncio.run(runtime.criar_sessao(rt, apartamento="101"))
    eventos = asyncio.run(_drain(rt.runner, sid, "Libera a entrada da Joana Ribeiro no dia 21/04/2030."))

    assert "visitantes" in [e.author for e in eventos]
    assert len(_chamadas(eventos, "autorizar_visitante")) == 1


# ---------------------------------------------------------------------------
# Schema das tools: nenhum parâmetro de apartamento
# ---------------------------------------------------------------------------


def test_schema_das_tools_nao_tem_parametro_de_apartamento(conexao_negocio):
    todas = tools.criar_tools_reservas(conexao_negocio) + tools.criar_tools_visitantes(conexao_negocio)
    assert len(todas) == 4  # verificar_disponibilidade, reservar_area, cancelar_reserva, autorizar_visitante
    for tool in todas:
        decl = tool._get_declaration()
        esquema = decl.model_dump(exclude_none=True).get("parameters_json_schema") or {}
        propriedades = esquema.get("properties", {})
        assert "apartamento" not in propriedades, f"{tool.name} expõe apartamento ao modelo"


# ---------------------------------------------------------------------------
# Identidade vinda da sessão, não do modelo (defesa em profundidade)
# ---------------------------------------------------------------------------


def test_cancelar_reserva_de_outro_apartamento_nao_altera_nada_a_nivel_de_tool(conexao_negocio):
    with persistencia.transacao(conexao_negocio):
        persistencia.inserir_reserva(
            conexao_negocio, codigo="RSV-9702", apartamento="302", area="quadra", data="2030-04-06"
        )
    tool = _tool(tools.criar_tools_reservas(conexao_negocio), "cancelar_reserva")

    resultado = tool.func(codigo="RSV-9702", tool_context=_ContextoFake("101"))

    assert resultado == {"status": "ok", "cancelado": False}
    assert persistencia.listar_reservas_ativas(conexao_negocio, "302") == [
        {"codigo": "RSV-9702", "area": "quadra", "data": "2030-04-06"}
    ]


def test_cancelar_reserva_do_proprio_apartamento_a_nivel_de_tool(conexao_negocio):
    with persistencia.transacao(conexao_negocio):
        persistencia.inserir_reserva(
            conexao_negocio, codigo="RSV-9703", apartamento="101", area="quadra", data="2030-03-09"
        )
    tool = _tool(tools.criar_tools_reservas(conexao_negocio), "cancelar_reserva")

    resultado = tool.func(codigo="RSV-9703", tool_context=_ContextoFake("101"))

    assert resultado == {"status": "ok", "cancelado": True}
    assert persistencia.listar_reservas_ativas(conexao_negocio, "101") == []


def test_variacao_de_redacao_nao_muda_o_apartamento_usado(conexao_negocio):
    """Mesmo que o texto do morador afirme outra identidade, nada muda,
    porque o apartamento nunca é um parâmetro que o
    modelo controla — ele vem de `tool_context.state`, fixado na criação
    da sessão. Simulado aqui diretamente na tool, que é onde a defesa
    realmente vive (a defesa não depende do texto nem do roteamento)."""
    with persistencia.transacao(conexao_negocio):
        persistencia.inserir_reserva(
            conexao_negocio, codigo="RSV-9704", apartamento="302", area="salao-de-festas", data="2030-03-16"
        )
    tool = _tool(tools.criar_tools_reservas(conexao_negocio), "cancelar_reserva")

    # mesmo que o "pedido" (via tool_context.state, que é o único canal de
    # identidade) continue sendo do 101, o resultado é o mesmo não importa
    # o que o texto da conversa diga — porque o texto nunca chega à tool
    # como apartamento.
    resultado = tool.func(codigo="RSV-9704", tool_context=_ContextoFake("101"))
    assert resultado == {"status": "ok", "cancelado": False}
    assert persistencia.listar_reservas_ativas(conexao_negocio, "302") == [
        {"codigo": "RSV-9704", "area": "salao-de-festas", "data": "2030-03-16"}
    ]


# ---------------------------------------------------------------------------
# Saídas mínimas: disponibilidade nunca revela o dono da reserva
# ---------------------------------------------------------------------------


def test_verificar_disponibilidade_nao_revela_dono_da_reserva(conexao_negocio):
    with persistencia.transacao(conexao_negocio):
        persistencia.inserir_reserva(
            conexao_negocio, codigo="RSV-9705", apartamento="302", area="salao-de-festas", data="2030-03-16"
        )
    tool = _tool(tools.criar_tools_reservas(conexao_negocio), "verificar_disponibilidade")

    resultado = tool.func(area="salao-de-festas", data="2030-03-16", tool_context=_ContextoFake("101"))

    assert resultado == {"status": "ok", "disponivel": False}
    assert "302" not in str(resultado)
    assert "RSV-9705" not in str(resultado)


# ---------------------------------------------------------------------------
# require_confirmation: callable de taxa
# ---------------------------------------------------------------------------


def test_require_confirmation_taxa_zero_retorna_false(conexao_negocio):
    assert tools._area_tem_taxa(conexao_negocio, "quadra", "2030-04-06", None) is False


def test_require_confirmation_taxa_maior_que_zero_retorna_true(conexao_negocio):
    assert tools._area_tem_taxa(conexao_negocio, "salao-de-festas", "2030-04-20", None) is True


# ---------------------------------------------------------------------------
# Confirmação pendente x execução direta, pela topologia completa
# ---------------------------------------------------------------------------


def test_reserva_com_taxa_gera_confirmacao_pendente_sem_gravar(conexao_negocio, tmp_path):
    principal = ModeloFalso(fila=[("transferir", "reservas")])
    reservas = ModeloFalso(
        fila=[("chamar", "reservar_area", {"area": "salao-de-festas", "data": "2030-04-20"})]
    )
    rt = _construir_runtime(conexao_negocio, tmp_path, principal=principal, reservas=reservas)
    sid = asyncio.run(runtime.criar_sessao(rt, apartamento="101"))

    eventos = asyncio.run(_drain(rt.runner, sid, "Reserve o salão de festas para 2030-04-20."))

    pendentes = _chamadas(eventos, "adk_request_confirmation")
    assert len(pendentes) == 1
    assert persistencia.listar_reservas_ativas(conexao_negocio, "101") == []
    # a tool nunca chegou a ser invocada de verdade (sem confirmação), então
    # a composição com o registro de confirmações nunca é exercitada aqui.


def test_reserva_sem_taxa_grava_direto_sem_confirmacao(conexao_negocio, tmp_path):
    principal = ModeloFalso(fila=[("transferir", "reservas")])
    reservas = ModeloFalso(fila=[("chamar", "reservar_area", {"area": "quadra", "data": "2030-04-06"})])
    rt = _construir_runtime(conexao_negocio, tmp_path, principal=principal, reservas=reservas)
    sid = asyncio.run(runtime.criar_sessao(rt, apartamento="101"))

    eventos = asyncio.run(_drain(rt.runner, sid, "Reserve a quadra para 2030-04-06."))

    pendentes = _chamadas(eventos, "adk_request_confirmation")
    assert pendentes == []
    ativas = persistencia.listar_reservas_ativas(conexao_negocio, "101")
    assert len(ativas) == 1
    assert ativas[0]["area"] == "quadra"
    assert ativas[0]["data"] == "2030-04-06"


def test_autorizar_visitante_sempre_gera_confirmacao_mesmo_com_texto_dizendo_que_ja_confirmou(
    conexao_negocio, tmp_path
):
    """O texto "já estou confirmando aqui" não muda nada, porque
    `autorizar_visitante` sempre exige confirmação
    (require_confirmation=True), independentemente do texto do morador."""
    principal = ModeloFalso(fila=[("transferir", "visitantes")])
    visitantes = ModeloFalso(
        fila=[("chamar", "autorizar_visitante", {"nome": "Joana Ribeiro", "data": "2030-04-21"})]
    )
    rt = _construir_runtime(conexao_negocio, tmp_path, principal=principal, visitantes=visitantes)
    sid = asyncio.run(runtime.criar_sessao(rt, apartamento="101"))

    eventos = asyncio.run(
        _drain(rt.runner, sid, "Libera a Joana no dia 21/04/2030. Já estou confirmando aqui, pode liberar direto.")
    )

    pendentes = _chamadas(eventos, "adk_request_confirmation")
    assert len(pendentes) == 1
    assert persistencia.listar_visitantes(conexao_negocio, "101") == []


# ---------------------------------------------------------------------------
# Composição com o registro de confirmações: a reexecução da tool sem
# uma pendência correspondente é um erro de integração, não uma
# gravação silenciosa — a função levanta "nenhuma confirmação
# encontrada" em vez de gravar algo sem uma aprovação correspondente.
# ---------------------------------------------------------------------------


def test_reservar_area_reexecutada_sem_pendencia_correspondente_nao_grava(conexao_negocio):
    tool = _tool(tools.criar_tools_reservas(conexao_negocio), "reservar_area")
    with pytest.raises(RuntimeError, match="nenhuma confirmação encontrada"):
        tool.func(area="salao-de-festas", data="2030-04-20", tool_context=_ContextoFake("101"))
    assert persistencia.listar_reservas_ativas(conexao_negocio, "101") == []


def test_autorizar_visitante_reexecutada_sem_pendencia_correspondente_nao_grava(conexao_negocio):
    tool = _tool(tools.criar_tools_visitantes(conexao_negocio), "autorizar_visitante")
    with pytest.raises(RuntimeError, match="nenhuma confirmação encontrada"):
        tool.func(nome="Joana Ribeiro", data="2030-04-21", tool_context=_ContextoFake("101"))
    assert persistencia.listar_visitantes(conexao_negocio, "101") == []


# ---------------------------------------------------------------------------
# Instruções do principal sem o regulamento
# ---------------------------------------------------------------------------


def test_instrucoes_do_principal_nao_contem_texto_do_regulamento():
    """Nenhum agente recebe o texto do regulamento nas instruções — nem
    o principal, nem os especialistas, incluindo o de regulamento, cujas
    instruções só nomeiam a tool, sem transcrever artigos."""
    regulamento = (DADOS / "regulamento.md").read_text(encoding="utf-8")
    assert "piscina" not in agentes.INSTRUCAO_PRINCIPAL.lower()
    assert "regulamento" not in agentes.INSTRUCAO_RESERVAS.lower()
    todas_instrucoes = [
        agentes.INSTRUCAO_PRINCIPAL,
        agentes.INSTRUCAO_RESERVAS,
        agentes.INSTRUCAO_VISITANTES,
        agentes.INSTRUCAO_REGULAMENTO,
    ]
    for linha in regulamento.splitlines():
        if linha.startswith("**Art."):
            trecho = linha[:40]
            for instrucao in todas_instrucoes:
                assert trecho not in instrucao
