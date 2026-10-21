"""Testes de integração da resolução de `area` nas tools de reservas,
com um modelo falso roteirizado, determinístico e sem rede.
`persistencia.resolver_area` e o uso dela em
`tools.criar_tools_reservas` nunca devem tratar uma área desconhecida
ou ambígua como gratuita, nem criar reserva ou tocar em qualquer estado
de confirmação para uma área inválida.
"""

from __future__ import annotations

import sqlite3

import pytest

from aurora import persistencia, tools


class _ContextoFake:
    """Mesmo stub mínimo usado em `test_topologia.py`."""

    def __init__(self, apartamento: str, *, function_call_id: str = "fc-inexistente"):
        self.state = {"apartamento": apartamento}
        self.function_call_id = function_call_id


def _tool(tools_list, nome):
    return next(t for t in tools_list if t.name == nome)


@pytest.fixture
def conexao_negocio(tmp_path):
    caminho = str(tmp_path / "app.db")
    conn = persistencia.conectar(caminho)
    persistencia.inicializar_esquema(conn, codigo_inicial=7000)
    conn.execute("INSERT INTO areas (id, nome, taxa) VALUES ('quadra', 'Quadra poliesportiva', 0)")
    conn.execute(
        "INSERT INTO areas (id, nome, taxa) VALUES ('salao-de-festas', 'Salão de festas', 150.0)"
    )
    conn.commit()
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# persistencia.resolver_area — id canônico, nome legível, desconhecida, ambígua
# ---------------------------------------------------------------------------


def test_resolver_area_por_id_canonico(conexao_negocio):
    linha, motivo = persistencia.resolver_area(conexao_negocio, "salao-de-festas")
    assert motivo is None
    assert linha["id"] == "salao-de-festas"
    assert linha["taxa"] == 150.0


def test_resolver_area_por_nome_legivel_com_acento_e_caixa_diferentes(conexao_negocio):
    """O modelo real pode enviar 'salão de festas' em vez do id canônico.
    A correspondência deve ser exata depois de normalizar
    acento/caixa/espaços — nunca parcial."""
    linha, motivo = persistencia.resolver_area(conexao_negocio, "SALÃO DE FESTAS")
    assert motivo is None
    assert linha["id"] == "salao-de-festas"

    linha2, motivo2 = persistencia.resolver_area(conexao_negocio, "  Salão de Festas  ")
    assert motivo2 is None
    assert linha2["id"] == "salao-de-festas"


def test_resolver_area_desconhecida(conexao_negocio):
    linha, motivo = persistencia.resolver_area(conexao_negocio, "piscina")
    assert linha is None
    assert motivo == "area_desconhecida"


def test_resolver_area_nao_faz_correspondencia_parcial(conexao_negocio):
    """'salão' não deve casar com 'Salão de festas' — só correspondência
    exata (depois de normalizar) é aceita, para nunca inventar uma
    correspondência."""
    linha, motivo = persistencia.resolver_area(conexao_negocio, "salão")
    assert linha is None
    assert motivo == "area_desconhecida"


def test_resolver_area_ambigua():
    """Duas áreas cujo nome normalizado colide devem ser tratadas como
    ambíguas, nunca resolvidas para uma delas por acaso."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE areas (id TEXT PRIMARY KEY, nome TEXT, taxa REAL)")
    conn.execute("INSERT INTO areas VALUES ('salao-de-festas', 'Salão de Festas', 150.0)")
    conn.execute("INSERT INTO areas VALUES ('salao-de-festas-2', 'salao de festas', 150.0)")
    conn.commit()

    linha, motivo = persistencia.resolver_area(conn, "Salao De Festas")
    assert linha is None
    assert motivo == "area_ambigua"


# ---------------------------------------------------------------------------
# tools.reservar_area / verificar_disponibilidade — comportamento fim a fim
# ---------------------------------------------------------------------------


def test_reservar_area_com_nome_legivel_de_area_paga_fica_pendente(conexao_negocio):
    """Pedir a área paga pelo nome em português deve exigir confirmação
    — nunca gravar direto."""
    tools_list = tools.criar_tools_reservas(conexao_negocio)
    reservar = _tool(tools_list, "reservar_area")

    precisa_confirmacao = tools._area_tem_taxa(
        conexao_negocio, "salão de festas", "2030-04-20", None
    )
    assert precisa_confirmacao is True

    # Sem confirmação (ADK não chamaria a tool direto, mas o teste chama
    # para garantir que a função também não grava nada se for invocada
    # fora do caminho de confirmação concedida — nunca deveria acontecer
    # na prática, mas o invariante vale mesmo assim: taxa > 0 sempre
    # delega para `_executar_reserva_confirmada`, que exige uma
    # confirmação 'aprovada' existente.
    with pytest.raises(RuntimeError, match="nenhuma confirmação encontrada"):
        reservar.func(area="salão de festas", data="2030-04-20", tool_context=_ContextoFake("101"))
    assert persistencia.listar_reservas_ativas(conexao_negocio, "101") == []


def test_reservar_area_com_id_canonico_de_area_gratuita_grava_direto(conexao_negocio):
    tools_list = tools.criar_tools_reservas(conexao_negocio)
    reservar = _tool(tools_list, "reservar_area")

    resultado = reservar.func(area="quadra", data="2030-04-06", tool_context=_ContextoFake("101"))

    assert resultado["status"] == "ok"
    assert resultado["reservado"] is True
    assert resultado["area"] == "quadra"
    reservas = persistencia.listar_reservas_ativas(conexao_negocio, "101")
    assert len(reservas) == 1
    assert reservas[0]["area"] == "quadra"


def test_reservar_area_com_nome_legivel_de_area_gratuita_grava_com_id_canonico(conexao_negocio):
    """Mesmo quando a área é gratuita, o nome legível deve ser resolvido
    para o id canônico antes de gravar — nunca grava a string crua."""
    tools_list = tools.criar_tools_reservas(conexao_negocio)
    reservar = _tool(tools_list, "reservar_area")

    resultado = reservar.func(
        area="Quadra Poliesportiva", data="2030-04-06", tool_context=_ContextoFake("101")
    )

    assert resultado["status"] == "ok"
    assert resultado["reservado"] is True
    assert resultado["area"] == "quadra"
    reservas = persistencia.listar_reservas_ativas(conexao_negocio, "101")
    assert reservas[0]["area"] == "quadra"


def test_reservar_area_desconhecida_nao_cria_reserva_nem_confirmacao(conexao_negocio):
    """O invariante central da correção: uma área inexistente nunca é
    tratada como gratuita. Nenhuma reserva é criada, e nenhuma linha é
    adicionada à tabela de confirmações."""
    tools_list = tools.criar_tools_reservas(conexao_negocio)
    reservar = _tool(tools_list, "reservar_area")

    precisa_confirmacao = tools._area_tem_taxa(conexao_negocio, "piscina", "2030-04-06", None)
    assert precisa_confirmacao is False  # não força confirmação para uma área inválida

    resultado = reservar.func(area="piscina", data="2030-04-06", tool_context=_ContextoFake("101"))

    assert resultado == {
        "status": "erro",
        "motivo": "area_desconhecida",
        "mensagem": "área desconhecida: não corresponde a nenhum id ou nome oficial",
    }
    assert persistencia.listar_reservas_ativas(conexao_negocio, "101") == []
    cursor = conexao_negocio.execute("SELECT COUNT(*) FROM confirmacoes")
    assert cursor.fetchone()[0] == 0


def test_verificar_disponibilidade_com_nome_legivel(conexao_negocio):
    tools_list = tools.criar_tools_reservas(conexao_negocio)
    verificar = _tool(tools_list, "verificar_disponibilidade")

    resultado = verificar.func(
        area="Salão de festas", data="2030-04-20", tool_context=_ContextoFake("101")
    )
    assert resultado == {"status": "ok", "disponivel": True}


def test_verificar_disponibilidade_area_desconhecida_retorna_erro_estruturado(conexao_negocio):
    tools_list = tools.criar_tools_reservas(conexao_negocio)
    verificar = _tool(tools_list, "verificar_disponibilidade")

    resultado = verificar.func(area="piscina", data="2030-04-20", tool_context=_ContextoFake("101"))

    assert resultado == {
        "status": "erro",
        "motivo": "area_desconhecida",
        "mensagem": "área desconhecida: não corresponde a nenhum id ou nome oficial",
    }
