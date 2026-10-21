"""Testes unitários de `aurora.persistencia`.

Usam arquivos SQLite temporários reais (não `:memory:`), para que o modo
WAL e a reabertura de conexão (teste de durabilidade do contador) sejam
exercitados de verdade.
"""

from __future__ import annotations

import sqlite3

import pytest

from aurora import persistencia as p

CODIGO_INICIAL = 4822  # 1 + max(1377, 2950, 4821) dos códigos de dados/reservas.json


@pytest.fixture
def conn(tmp_path):
    caminho = str(tmp_path / "app.db")
    c = p.conectar(caminho)
    p.inicializar_esquema(c, codigo_inicial=CODIGO_INICIAL)
    yield c
    c.close()


# ---------------------------------------------------------------------------
# Esquema
# ---------------------------------------------------------------------------


def test_inicializar_esquema_e_idempotente(tmp_path):
    caminho = str(tmp_path / "app.db")
    c = p.conectar(caminho)
    p.inicializar_esquema(c, codigo_inicial=CODIGO_INICIAL)
    p.inicializar_esquema(c, codigo_inicial=999999)  # segunda chamada não deve resetar o contador
    row = c.execute("SELECT proximo FROM contador_codigo WHERE id = 1").fetchone()
    assert row["proximo"] == CODIGO_INICIAL
    c.close()


# ---------------------------------------------------------------------------
# Contador de códigos
# ---------------------------------------------------------------------------


def test_proximo_codigo_e_crescente(conn):
    codigos = [p.proximo_codigo(conn) for _ in range(5)]
    numeros = [int(c.split("-")[1]) for c in codigos]
    assert numeros == sorted(numeros)
    assert len(set(codigos)) == 5


def test_proximo_codigo_nunca_e_igual_a_codigo_existente(conn):
    existentes = {"RSV-1377", "RSV-2950", "RSV-4821"}
    for _ in range(10):
        assert p.proximo_codigo(conn) not in existentes


def test_contador_persiste_apos_reabrir_conexao(tmp_path):
    caminho = str(tmp_path / "app.db")
    c1 = p.conectar(caminho)
    p.inicializar_esquema(c1, codigo_inicial=CODIGO_INICIAL)
    primeiro = p.proximo_codigo(c1)
    segundo = p.proximo_codigo(c1)
    c1.close()

    c2 = p.conectar(caminho)
    p.inicializar_esquema(c2, codigo_inicial=CODIGO_INICIAL)  # idempotente; não deve voltar o contador
    terceiro = p.proximo_codigo(c2)
    c2.close()

    assert [primeiro, segundo, terceiro] == [
        f"RSV-{CODIGO_INICIAL}",
        f"RSV-{CODIGO_INICIAL + 1}",
        f"RSV-{CODIGO_INICIAL + 2}",
    ]


def test_codigo_de_reserva_cancelada_nao_e_reutilizado(conn):
    """Mesmo depois de cancelar, o código `codigo` não pode ser usado de
    novo — a chave primária de `reservas` garante isso independentemente
    do contador."""
    codigo = p.proximo_codigo(conn)
    with p.transacao(conn):
        p.inserir_reserva(conn, codigo=codigo, apartamento="101", area="quadra", data="2030-04-06")
    with p.transacao(conn):
        assert p.cancelar_reserva(conn, codigo=codigo, apartamento="101") is True

    with pytest.raises(sqlite3.IntegrityError):
        with p.transacao(conn):
            # tentativa de reinserir o mesmo código, em outra área/data: deve falhar pela PK.
            p.inserir_reserva(conn, codigo=codigo, apartamento="201", area="salao-de-festas", data="2030-05-01")


# ---------------------------------------------------------------------------
# Índice único parcial de reservas ativas
# ---------------------------------------------------------------------------


def test_segunda_reserva_ativa_na_mesma_area_e_data_e_recusada(conn):
    with p.transacao(conn):
        p.inserir_reserva(conn, codigo="RSV-9001", apartamento="101", area="salao-de-festas", data="2030-05-11")

    with pytest.raises(sqlite3.IntegrityError):
        with p.transacao(conn):
            p.inserir_reserva(conn, codigo="RSV-9002", apartamento="201", area="salao-de-festas", data="2030-05-11")

    # a reserva vencedora continua intacta, sem efeito da tentativa recusada.
    assert p.listar_reservas_ativas(conn, "101") == [
        {"codigo": "RSV-9001", "area": "salao-de-festas", "data": "2030-05-11"}
    ]
    assert p.listar_reservas_ativas(conn, "201") == []


def test_reservas_em_datas_ou_areas_diferentes_nao_colidem(conn):
    with p.transacao(conn):
        p.inserir_reserva(conn, codigo="RSV-9101", apartamento="101", area="quadra", data="2030-04-06")
        p.inserir_reserva(conn, codigo="RSV-9102", apartamento="101", area="quadra", data="2030-04-07")
        p.inserir_reserva(conn, codigo="RSV-9103", apartamento="101", area="churrasqueira", data="2030-04-06")
    assert len(p.listar_reservas_ativas(conn, "101")) == 3


def test_area_disponivel(conn):
    assert p.area_disponivel(conn, area="quadra", data="2030-04-06") is True
    with p.transacao(conn):
        p.inserir_reserva(conn, codigo="RSV-9201", apartamento="101", area="quadra", data="2030-04-06")
    assert p.area_disponivel(conn, area="quadra", data="2030-04-06") is False


# ---------------------------------------------------------------------------
# Cancelamento e isolamento por apartamento
# ---------------------------------------------------------------------------


def test_cancelar_reserva_do_proprio_apartamento(conn):
    with p.transacao(conn):
        p.inserir_reserva(conn, codigo="RSV-9301", apartamento="101", area="quadra", data="2030-03-09")
    with p.transacao(conn):
        assert p.cancelar_reserva(conn, codigo="RSV-9301", apartamento="101") is True
    assert p.listar_reservas_ativas(conn, "101") == []


def test_cancelar_reserva_de_outro_apartamento_nao_altera_nada(conn):
    """Tentar cancelar a reserva de outro apartamento não a altera."""
    with p.transacao(conn):
        p.inserir_reserva(conn, codigo="RSV-9401", apartamento="302", area="salao-de-festas", data="2030-03-16")
    with p.transacao(conn):
        resultado = p.cancelar_reserva(conn, codigo="RSV-9401", apartamento="101")
    assert resultado is False
    assert p.listar_reservas_ativas(conn, "302") == [
        {"codigo": "RSV-9401", "area": "salao-de-festas", "data": "2030-03-16"}
    ]


def test_cancelar_reserva_ja_cancelada_nao_altera_nada(conn):
    with p.transacao(conn):
        p.inserir_reserva(conn, codigo="RSV-9501", apartamento="101", area="quadra", data="2030-03-09")
    with p.transacao(conn):
        assert p.cancelar_reserva(conn, codigo="RSV-9501", apartamento="101") is True
    with p.transacao(conn):
        assert p.cancelar_reserva(conn, codigo="RSV-9501", apartamento="101") is False


@pytest.mark.skip(
    reason=(
        "Decisão de produto ainda não tomada: se uma reserva cancelada "
        "deve mesmo liberar a área e a data para uma NOVA reserva. O "
        "índice único parcial permite esse reuso como consequência do "
        "desenho do índice, mas isso não é a mesma coisa que alguém ter "
        "aprovado esse comportamento como regra de negócio. Este teste "
        "fica escrito e visível, mas não executado, até a decisão ser "
        "tomada."
    )
)
def test_reservar_em_data_liberada_por_cancelamento_bloqueado_por_decisao_de_produto(conn):
    with p.transacao(conn):
        p.inserir_reserva(conn, codigo="RSV-9601", apartamento="101", area="quadra", data="2030-04-06")
    with p.transacao(conn):
        p.cancelar_reserva(conn, codigo="RSV-9601", apartamento="101")
    with p.transacao(conn):
        p.inserir_reserva(conn, codigo="RSV-9602", apartamento="201", area="quadra", data="2030-04-06")
    assert p.listar_reservas_ativas(conn, "201") == [
        {"codigo": "RSV-9602", "area": "quadra", "data": "2030-04-06"}
    ]


# ---------------------------------------------------------------------------
# Visitantes
# ---------------------------------------------------------------------------


def test_autorizar_e_listar_visitante(conn):
    with p.transacao(conn):
        p.autorizar_visitante(conn, apartamento="302", nome="Marina Duarte", data="2030-03-16")
    assert p.listar_visitantes(conn, "302") == [{"nome": "Marina Duarte", "data": "2030-03-16"}]
    assert p.listar_visitantes(conn, "101") == []


# ---------------------------------------------------------------------------
# Registro de confirmações: transições válidas
# ---------------------------------------------------------------------------


def _criar_pendente(conn, id="c1", session_id="s1", apartamento="101"):
    with p.transacao(conn):
        p.criar_confirmacao_pendente(
            conn,
            id=id,
            session_id=session_id,
            apartamento=apartamento,
            acao="reservar_area",
            area="salao-de-festas",
            data="2030-04-20",
            original_function_call_id="fc-original-1",
        )


def test_transicao_pendente_para_aprovada(conn):
    _criar_pendente(conn)
    with p.transacao(conn):
        ok = p.transicionar_confirmacao(conn, id="c1", session_id="s1", origem="pendente", destino="aprovada")
    assert ok is True
    conf = p.obter_confirmacao(conn, id="c1", session_id="s1")
    assert conf["estado"] == "aprovada"
    assert conf["decidida_em"] is not None


def test_transicao_pendente_para_negada(conn):
    _criar_pendente(conn)
    with p.transacao(conn):
        ok = p.transicionar_confirmacao(conn, id="c1", session_id="s1", origem="pendente", destino="negada")
    assert ok is True
    assert p.obter_confirmacao(conn, id="c1", session_id="s1")["estado"] == "negada"


def test_transicao_aprovada_para_executada(conn):
    _criar_pendente(conn)
    with p.transacao(conn):
        p.transicionar_confirmacao(conn, id="c1", session_id="s1", origem="pendente", destino="aprovada")
    with p.transacao(conn):
        ok = p.transicionar_confirmacao(conn, id="c1", session_id="s1", origem="aprovada", destino="executada")
    assert ok is True
    conf = p.obter_confirmacao(conn, id="c1", session_id="s1")
    assert conf["estado"] == "executada"
    assert conf["executada_em"] is not None


def test_transicao_aprovada_para_falhou_com_motivo(conn):
    _criar_pendente(conn)
    with p.transacao(conn):
        p.transicionar_confirmacao(conn, id="c1", session_id="s1", origem="pendente", destino="aprovada")
    with p.transacao(conn):
        ok = p.transicionar_confirmacao(
            conn, id="c1", session_id="s1", origem="aprovada", destino="falhou", motivo="data_ocupada"
        )
    assert ok is True
    conf = p.obter_confirmacao(conn, id="c1", session_id="s1")
    assert conf["estado"] == "falhou"
    assert conf["motivo"] == "data_ocupada"


def test_transicao_aprovada_para_interrompida(conn):
    _criar_pendente(conn)
    with p.transacao(conn):
        p.transicionar_confirmacao(conn, id="c1", session_id="s1", origem="pendente", destino="aprovada")
    with p.transacao(conn):
        ok = p.transicionar_confirmacao(conn, id="c1", session_id="s1", origem="aprovada", destino="interrompida")
    assert ok is True
    assert p.obter_confirmacao(conn, id="c1", session_id="s1")["estado"] == "interrompida"


# ---------------------------------------------------------------------------
# Registro de confirmações: transições inválidas (reenvio)
# ---------------------------------------------------------------------------


def test_reenvio_de_confirmacao_ja_aprovada_nao_executa_de_novo(conn):
    """Reenviar a aprovação de um `id` já aprovado (ou já respondido de
    outra forma) não altera nada — o chamador deve responder 409 sem
    jamais chamar o ADK."""
    _criar_pendente(conn)
    with p.transacao(conn):
        assert p.transicionar_confirmacao(conn, id="c1", session_id="s1", origem="pendente", destino="aprovada") is True
    with p.transacao(conn):
        reenvio = p.transicionar_confirmacao(conn, id="c1", session_id="s1", origem="pendente", destino="aprovada")
    assert reenvio is False
    assert p.obter_confirmacao(conn, id="c1", session_id="s1")["estado"] == "aprovada"


def test_aprovar_depois_de_negar_nao_executa(conn):
    """Negar e depois tentar aprovar o mesmo `id` não executa."""
    _criar_pendente(conn)
    with p.transacao(conn):
        p.transicionar_confirmacao(conn, id="c1", session_id="s1", origem="pendente", destino="negada")
    with p.transacao(conn):
        tentativa = p.transicionar_confirmacao(conn, id="c1", session_id="s1", origem="pendente", destino="aprovada")
    assert tentativa is False
    assert p.obter_confirmacao(conn, id="c1", session_id="s1")["estado"] == "negada"


def test_transicao_com_sessao_errada_nao_executa(conn):
    """Responder numa sessão B o `id` de uma confirmação pendente da
    sessão A não executa nada, e a confirmação continua pendente em A."""
    _criar_pendente(conn, id="c1", session_id="sA")
    with p.transacao(conn):
        resultado = p.transicionar_confirmacao(conn, id="c1", session_id="sB", origem="pendente", destino="aprovada")
    assert resultado is False
    assert p.obter_confirmacao(conn, id="c1", session_id="sA")["estado"] == "pendente"


def test_transicao_com_id_inexistente_nao_executa(conn):
    with p.transacao(conn):
        resultado = p.transicionar_confirmacao(
            conn, id="id-inexistente", session_id="s1", origem="pendente", destino="aprovada"
        )
    assert resultado is False


def test_transicao_invalida_levanta_value_error(conn):
    """Transições fora da máquina de estados são erro de uso do
    chamador, não condição de corrida."""
    with pytest.raises(ValueError):
        p.transicionar_confirmacao(conn, id="c1", session_id="s1", origem="executada", destino="aprovada")
    with pytest.raises(ValueError):
        p.transicionar_confirmacao(conn, id="c1", session_id="s1", origem="negada", destino="executada")


# ---------------------------------------------------------------------------
# Pendências e consultas
# ---------------------------------------------------------------------------


def test_duas_pendencias_na_mesma_sessao_aparecem_as_duas(conn):
    _criar_pendente(conn, id="c1", session_id="s1")
    with p.transacao(conn):
        p.criar_confirmacao_pendente(
            conn,
            id="c2",
            session_id="s1",
            apartamento="101",
            acao="autorizar_visitante",
            nome="Joana Ribeiro",
            data="2030-04-21",
            original_function_call_id="fc-original-2",
        )
    pendentes = p.listar_pendentes(conn, "s1")
    assert {c["id"] for c in pendentes} == {"c1", "c2"}

    # cada uma só é afetada pela resposta ao seu próprio id.
    with p.transacao(conn):
        p.transicionar_confirmacao(conn, id="c1", session_id="s1", origem="pendente", destino="aprovada")
    pendentes_apos = p.listar_pendentes(conn, "s1")
    assert [c["id"] for c in pendentes_apos] == ["c2"]


def test_id_de_confirmacoes_pendentes_e_aceito_pela_rota_de_confirmacoes(conn):
    """No nível de repositório, o `id` criado é o mesmo que a transição
    aceita."""
    _criar_pendente(conn, id="c-exposto")
    pendentes = p.listar_pendentes(conn, "s1")
    assert pendentes[0]["id"] == "c-exposto"
    with p.transacao(conn):
        assert p.transicionar_confirmacao(
            conn, id=pendentes[0]["id"], session_id="s1", origem="pendente", destino="aprovada"
        ) is True


def test_obter_confirmacao_por_function_call_id(conn):
    """Usado para localizar a confirmação `aprovada` a partir do
    `function_call_id` que a tool recebe na reexecução."""
    _criar_pendente(conn)
    conf = p.obter_confirmacao_por_function_call_id(conn, original_function_call_id="fc-original-1")
    assert conf["id"] == "c1"


def test_listar_aprovadas_sem_execucao(conn):
    _criar_pendente(conn, id="c1")
    with p.transacao(conn):
        p.transicionar_confirmacao(conn, id="c1", session_id="s1", origem="pendente", destino="aprovada")
    aprovadas = p.listar_aprovadas_sem_execucao(conn)
    assert [c["id"] for c in aprovadas] == ["c1"]

    with p.transacao(conn):
        p.transicionar_confirmacao(conn, id="c1", session_id="s1", origem="aprovada", destino="executada")
    assert p.listar_aprovadas_sem_execucao(conn) == []


# ---------------------------------------------------------------------------
# Composição atômica: gravar a reserva e marcar executada juntas
# ---------------------------------------------------------------------------


def test_execucao_e_gravacao_sao_atomicas_em_sucesso(conn):
    """Simula a composição real: dentro de UMA transação, inserir a
    reserva e marcar a confirmação como executada."""
    _criar_pendente(conn)
    with p.transacao(conn):
        p.transicionar_confirmacao(conn, id="c1", session_id="s1", origem="pendente", destino="aprovada")

    codigo = p.proximo_codigo(conn)
    with p.transacao(conn):
        p.inserir_reserva(conn, codigo=codigo, apartamento="101", area="salao-de-festas", data="2030-04-20")
        ok = p.transicionar_confirmacao(conn, id="c1", session_id="s1", origem="aprovada", destino="executada")
    assert ok is True
    assert p.listar_reservas_ativas(conn, "101") == [
        {"codigo": codigo, "area": "salao-de-festas", "data": "2030-04-20"}
    ]
    assert p.obter_confirmacao(conn, id="c1", session_id="s1")["estado"] == "executada"


def test_execucao_desfaz_reserva_quando_transicao_falha_dentro_da_mesma_transacao(conn):
    """Se a transição para `executada` não for válida (por exemplo, a
    confirmação não estava mais `aprovada`), a composição com
    `inserir_reserva` deve ser desfeita junto — ambas na mesma
    `transacao()`, então um retorno `False` da transição deve levar o
    chamador a levantar e provocar o rollback da reserva também."""
    _criar_pendente(conn)
    with p.transacao(conn):
        p.transicionar_confirmacao(conn, id="c1", session_id="s1", origem="pendente", destino="negada")

    codigo = p.proximo_codigo(conn)
    with pytest.raises(RuntimeError):
        with p.transacao(conn):
            p.inserir_reserva(conn, codigo=codigo, apartamento="101", area="salao-de-festas", data="2030-04-20")
            ok = p.transicionar_confirmacao(conn, id="c1", session_id="s1", origem="aprovada", destino="executada")
            if not ok:
                raise RuntimeError("confirmação não estava aprovada; desfazendo a reserva")

    # a reserva não deve existir, porque a transação inteira foi desfeita.
    assert p.listar_reservas_ativas(conn, "101") == []
