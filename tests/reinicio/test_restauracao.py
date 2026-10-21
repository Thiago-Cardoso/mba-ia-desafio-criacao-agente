"""Testes da restauração e da trava de exclusividade, sem processo
real — o reinício real da API está em `test_reinicio_real_api.py`,
neste mesmo diretório."""

from __future__ import annotations

from pathlib import Path

import pytest

from aurora import dados_iniciais, persistencia
from aurora import restaurar as restaurar_mod
from aurora.trava import TravaOcupadaError, trava_exclusiva

DADOS = Path(__file__).resolve().parents[2] / "dados"


@pytest.fixture
def app_db(tmp_path):
    caminho = str(tmp_path / "app.db")
    conn = persistencia.conectar(caminho)
    persistencia.inicializar_esquema(conn, codigo_inicial=4822)
    conn.commit()
    yield caminho, conn
    conn.close()


def _contador_atual(conn) -> int:
    return conn.execute("SELECT proximo FROM contador_codigo WHERE id = 1").fetchone()["proximo"]


# ---------------------------------------------------------------------------
# Carga inicial (apartamentos, áreas) — base para `subir`
# ---------------------------------------------------------------------------


def test_maior_numero_de_codigo_a_partir_de_dados():
    reservas = dados_iniciais.carregar_reservas_iniciais(str(DADOS))
    assert dados_iniciais.maior_numero_de_codigo(reservas) == 4821


def test_carregar_apartamentos_e_areas_e_idempotente(app_db):
    caminho, conn = app_db
    apartamentos = dados_iniciais.carregar_apartamentos(str(DADOS))
    areas = dados_iniciais.carregar_areas(str(DADOS))

    persistencia.carregar_apartamentos_e_areas(conn, apartamentos=apartamentos, areas=areas)
    row = conn.execute("SELECT morador FROM apartamentos WHERE numero = '101'").fetchone()
    assert row["morador"] == "Helena Prado"

    # chamar de novo (como `subir` faz em toda subida) não duplica nem falha.
    persistencia.carregar_apartamentos_e_areas(conn, apartamentos=apartamentos, areas=areas)
    total = conn.execute("SELECT COUNT(*) AS n FROM apartamentos").fetchone()["n"]
    assert total == len(apartamentos)


# ---------------------------------------------------------------------------
# Restauração
# ---------------------------------------------------------------------------


def test_restaurar_recria_reservas_e_visitantes_originais(app_db):
    caminho, conn = app_db
    with persistencia.transacao(conn):
        persistencia.inserir_reserva(
            conn, codigo="RSV-9999", apartamento="999", area="quadra", data="2031-01-01"
        )
        persistencia.autorizar_visitante(conn, apartamento="999", nome="Fulano de Tal", data="2031-01-01")

    resultado = restaurar_mod.restaurar(dir_dados=str(DADOS), caminho_app_db=caminho)

    assert resultado["reservas_removidas"] == 1
    assert resultado["reservas_recriadas"] == 3
    assert resultado["visitantes_removidos"] == 1
    assert resultado["visitantes_recriados"] == 2
    assert persistencia.listar_reservas_ativas(conn, "101") == [
        {"codigo": "RSV-1377", "area": "quadra", "data": "2030-03-09"}
    ]
    assert persistencia.listar_visitantes(conn, "302") == [{"nome": "Marina Duarte", "data": "2030-03-16"}]
    # a reserva e o visitante ad hoc do apartamento 999 não existem em dados/
    # e não sobrevivem à restauração.
    assert persistencia.listar_reservas_ativas(conn, "999") == []
    assert persistencia.listar_visitantes(conn, "999") == []


def test_restaurar_funciona_em_clone_limpo_sem_subir_antes(tmp_path):
    """`restaurar` não pode presumir que `subir` já criou o esquema: os
    dois comandos existem separadamente, sem ordem obrigatória entre
    eles. Aqui nem o diretório `data/` equivalente, nem o arquivo do
    banco, existem antes da chamada."""
    caminho = str(tmp_path / "ainda-nao-existe" / "app.db")

    resultado = restaurar_mod.restaurar(dir_dados=str(DADOS), caminho_app_db=caminho)

    assert resultado["reservas_recriadas"] == 3
    assert resultado["visitantes_recriados"] == 2
    conn = persistencia.conectar(caminho)
    try:
        assert persistencia.listar_reservas_ativas(conn, "101") == [
            {"codigo": "RSV-1377", "area": "quadra", "data": "2030-03-09"}
        ]
    finally:
        conn.close()


def test_restaurar_recusa_com_confirmacao_aprovada_sem_execucao(app_db):
    """Sai com erro, sem alterar nada, se houver confirmação em
    'aprovada' (operação incompleta)."""
    caminho, conn = app_db
    with persistencia.transacao(conn):
        persistencia.criar_confirmacao_pendente(
            conn,
            id="c1",
            session_id="s1",
            apartamento="101",
            acao="reservar_area",
            area="salao-de-festas",
            data="2030-04-20",
        )
        persistencia.transicionar_confirmacao(
            conn, id="c1", session_id="s1", origem="pendente", destino="aprovada"
        )

    with pytest.raises(restaurar_mod.RestauracaoRecusadaError):
        restaurar_mod.restaurar(dir_dados=str(DADOS), caminho_app_db=caminho)

    # nada foi alterado: a restauração nunca chegou a apagar/recriar nada.
    assert persistencia.listar_reservas_ativas(conn, "101") == []
    conf = persistencia.obter_confirmacao(conn, id="c1", session_id="s1")
    assert conf["estado"] == "aprovada"


def test_restaurar_relata_mas_nao_apaga_pendentes_e_nao_altera_contador(app_db):
    caminho, conn = app_db
    with persistencia.transacao(conn):
        persistencia.criar_confirmacao_pendente(
            conn,
            id="c2",
            session_id="s2",
            apartamento="101",
            acao="autorizar_visitante",
            nome="Zeca",
            data="2030-04-21",
        )
    contador_antes = _contador_atual(conn)

    resultado = restaurar_mod.restaurar(dir_dados=str(DADOS), caminho_app_db=caminho)

    assert resultado["confirmacoes_pendentes_relatadas"] == 1
    pendente = persistencia.obter_confirmacao(conn, id="c2", session_id="s2")
    assert pendente is not None
    assert pendente["estado"] == "pendente"  # relatada, não apagada
    assert _contador_atual(conn) == contador_antes  # o contador não é tocado


# ---------------------------------------------------------------------------
# Trava de exclusividade: só `subir` OU `restaurar` por vez
# ---------------------------------------------------------------------------


def test_trava_ocupada_impede_segunda_trava_no_mesmo_caminho(tmp_path):
    caminho_lock = tmp_path / "aurora.lock"
    with trava_exclusiva(caminho_lock):
        with pytest.raises(TravaOcupadaError):
            with trava_exclusiva(caminho_lock):
                pytest.fail("não deveria obter a trava enquanto a primeira está aberta")


def test_trava_e_liberada_ao_sair_do_bloco_e_pode_ser_obtida_de_novo(tmp_path):
    caminho_lock = tmp_path / "aurora.lock"
    with trava_exclusiva(caminho_lock):
        pass
    with trava_exclusiva(caminho_lock):
        pass  # não levanta: a primeira trava já foi liberada.
