"""Testes de injeção de falhas no fluxo de confirmação, com processo
real e falhas injetadas só por `tests/falhas/` (isolamento verificado
em `test_isolamento.py`).

Cada cenário sobe `tests/falhas/app_de_teste.py` com `AURORA_FALHA_MODO`
definido; a recuperação é verificada chamando a mesma função de
produção (`confirmacao.reconciliar_aprovadas_sem_execucao`) diretamente
sobre o arquivo SQLite real deixado pelo processo que caiu —
armazenamento real, nunca simulado em memória.
"""

from __future__ import annotations

import httpx
import pytest

from aurora import persistencia
from aurora import confirmacao as confirmacao_mod

from ._subprocesso import (
    esperar_processo_morrer,
    escrever_roteiro,
    parar_processo_normalmente,
    porta_livre,
    subir_processo,
)


@pytest.fixture
def bancos(tmp_path):
    return {
        "app_db": str(tmp_path / "app.db"),
        "adk_db": str(tmp_path / "adk.db"),
        "lock": str(tmp_path / "aurora.lock"),
        "tmp_path": tmp_path,
    }


def _roteiro_pedido_com_taxa(area="salao-de-festas", data="2030-04-20") -> dict:
    return {
        "principal": [["transferir", "reservas"]],
        "reservas": [["chamar", "reservar_area", {"area": area, "data": data}]],
    }


def _criar_pendencia(porta: int, *, apartamento="101", area="salao-de-festas", data="2030-04-20") -> tuple[str, str]:
    with httpx.Client(base_url=f"http://127.0.0.1:{porta}", timeout=10.0) as cliente:
        sid = cliente.post("/sessoes", json={"apartamento": apartamento}).json()["session_id"]
        resp = cliente.post(f"/sessoes/{sid}/mensagens", json={"texto": f"Reserve {area} para {data}."})
        pendencia_id = resp.json()["confirmacoes_pendentes"][0]["id"]
        return sid, pendencia_id


# ---------------------------------------------------------------------------
# Queda entre a aprovação e a execução da tool
# ---------------------------------------------------------------------------


def test_queda_entre_aprovacao_e_execucao_subida_marca_interrompida_sem_reserva(bancos):
    porta = porta_livre()
    roteiro = bancos["tmp_path"] / "roteiro.json"
    escrever_roteiro(roteiro, _roteiro_pedido_com_taxa())
    proc = subir_processo(
        porta=porta, app_db=bancos["app_db"], adk_db=bancos["adk_db"], lock=bancos["lock"],
        roteiro_arquivo=roteiro, falha_modo="queda_apos_x1",
    )
    sid, pendencia_id = _criar_pendencia(porta)

    with pytest.raises(httpx.TransportError):
        with httpx.Client(base_url=f"http://127.0.0.1:{porta}", timeout=5.0) as cliente:
            cliente.post(f"/sessoes/{sid}/confirmacoes", json={"id": pendencia_id, "confirmado": True})

    codigo_saida, saida = esperar_processo_morrer(proc)
    assert codigo_saida != 0, "o processo deveria ter caído (os._exit) para este cenário"

    conexao = persistencia.conectar(bancos["app_db"])
    try:
        conf = persistencia.obter_confirmacao(conexao, id=pendencia_id, session_id=sid)
        assert conf["estado"] == "aprovada"  # a aprovação comitou; a tool nunca executou.
        assert persistencia.listar_reservas_ativas(conexao, "101") == []

        interrompidas = confirmacao_mod.reconciliar_aprovadas_sem_execucao(conexao)
        assert interrompidas == [pendencia_id]
        conf_depois = persistencia.obter_confirmacao(conexao, id=pendencia_id, session_id=sid)
        assert conf_depois["estado"] == "interrompida"
        assert persistencia.listar_reservas_ativas(conexao, "101") == []
    finally:
        conexao.close()


# ---------------------------------------------------------------------------
# Queda depois do efeito de negócio gravado, antes da resposta HTTP
# ---------------------------------------------------------------------------


def test_queda_depois_do_commit_reserva_unica_e_reenvio_409(bancos):
    porta = porta_livre()
    roteiro = bancos["tmp_path"] / "roteiro.json"
    escrever_roteiro(roteiro, _roteiro_pedido_com_taxa())
    proc = subir_processo(
        porta=porta, app_db=bancos["app_db"], adk_db=bancos["adk_db"], lock=bancos["lock"],
        roteiro_arquivo=roteiro, falha_modo="queda_apos_commit",
    )
    sid, pendencia_id = _criar_pendencia(porta)

    with pytest.raises(httpx.TransportError):
        with httpx.Client(base_url=f"http://127.0.0.1:{porta}", timeout=5.0) as cliente:
            cliente.post(f"/sessoes/{sid}/confirmacoes", json={"id": pendencia_id, "confirmado": True})

    codigo_saida, saida = esperar_processo_morrer(proc)
    assert codigo_saida != 0

    conexao = persistencia.conectar(bancos["app_db"])
    try:
        conf = persistencia.obter_confirmacao(conexao, id=pendencia_id, session_id=sid)
        assert conf["estado"] == "executada"  # o efeito de negócio comitou antes da queda.
        ativas = persistencia.listar_reservas_ativas(conexao, "101")
        assert len(ativas) == 1
    finally:
        conexao.close()

    # Reenvio num processo novo (limpo, sem falha), real: 409, sem duplicar.
    roteiro_b = bancos["tmp_path"] / "roteiro_b.json"
    escrever_roteiro(roteiro_b, {})
    porta_b = porta_livre()
    proc_b = subir_processo(
        porta=porta_b, app_db=bancos["app_db"], adk_db=bancos["adk_db"], lock=bancos["lock"],
        roteiro_arquivo=roteiro_b,
    )
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{porta_b}", timeout=5.0) as cliente:
            resp = cliente.post(f"/sessoes/{sid}/confirmacoes", json={"id": pendencia_id, "confirmado": True})
            assert resp.status_code == 409
            ativas_depois = cliente.get("/apartamentos/101/reservas").json()
            assert len(ativas_depois) == 1
    finally:
        codigo_saida_b, saida_b = parar_processo_normalmente(proc_b)
    assert codigo_saida_b == 0, f"processo B não encerrou normalmente. Saída:\n{saida_b}"


# ---------------------------------------------------------------------------
# Excepção depois do efeito gravado (processo continua vivo): resposta real
# ---------------------------------------------------------------------------


def test_excecao_depois_do_commit_rota_responde_resultado_real(bancos):
    porta = porta_livre()
    roteiro = bancos["tmp_path"] / "roteiro.json"
    escrever_roteiro(roteiro, _roteiro_pedido_com_taxa())
    proc = subir_processo(
        porta=porta, app_db=bancos["app_db"], adk_db=bancos["adk_db"], lock=bancos["lock"],
        roteiro_arquivo=roteiro, falha_modo="excecao_apos_commit",
    )
    try:
        sid, pendencia_id = _criar_pendencia(porta)
        with httpx.Client(base_url=f"http://127.0.0.1:{porta}", timeout=10.0) as cliente:
            resp = cliente.post(f"/sessoes/{sid}/confirmacoes", json={"id": pendencia_id, "confirmado": True})
            # A excepção foi injetada DEPOIS do efeito de negócio já
            # gravado: a aplicação relê o registro (já 'executada') e
            # devolve sucesso, nunca a excepção nem uma marca de
            # 'falhou' indevida.
            assert resp.status_code == 200
            assert resp.json()["confirmacoes_pendentes"] == []

            ativas = cliente.get("/apartamentos/101/reservas").json()
            assert len(ativas) == 1
            assert ativas[0] == {"codigo": "RSV-4822", "area": "salao-de-festas", "data": "2030-04-20"}

            # Reenvio, ainda no MESMO processo: 409, sem duplicar.
            resp_reenvio = cliente.post(
                f"/sessoes/{sid}/confirmacoes", json={"id": pendencia_id, "confirmado": True}
            )
            assert resp_reenvio.status_code == 409
            assert cliente.get("/apartamentos/101/reservas").json() == ativas
    finally:
        codigo_saida, saida = parar_processo_normalmente(proc)
    assert codigo_saida == 0, f"processo não deveria ter caído neste cenário. Saída:\n{saida}"

    conexao = persistencia.conectar(bancos["app_db"])
    try:
        conf = persistencia.obter_confirmacao(conexao, id=pendencia_id, session_id=sid)
        assert conf["estado"] == "executada"
        assert conf["motivo"] is None  # nunca marcada 'falhou'.
    finally:
        conexao.close()


# ---------------------------------------------------------------------------
# ADK falha antes da tool: interrompida; 500 genérico
# ---------------------------------------------------------------------------


def test_adk_falha_antes_da_tool_500_generico_sem_reserva_e_reconciliacao(bancos):
    porta = porta_livre()
    roteiro = bancos["tmp_path"] / "roteiro.json"
    escrever_roteiro(roteiro, _roteiro_pedido_com_taxa())
    proc = subir_processo(
        porta=porta, app_db=bancos["app_db"], adk_db=bancos["adk_db"], lock=bancos["lock"],
        roteiro_arquivo=roteiro, falha_modo="antes_da_tool",
    )
    try:
        sid, pendencia_id = _criar_pendencia(porta)
        with httpx.Client(base_url=f"http://127.0.0.1:{porta}", timeout=10.0) as cliente:
            resp = cliente.post(f"/sessoes/{sid}/confirmacoes", json={"id": pendencia_id, "confirmado": True})
            assert resp.status_code == 500
            corpo = str(resp.json())
            assert "Traceback" not in corpo
            assert "RuntimeError" not in corpo
            assert "id_requisicao" in corpo

        # Nova conexão (não a mesma do POST que levou à excepção e ao `500`):
        # o Starlette relança a excepção depois de enviar a resposta
        # (`ServerErrorMiddleware`), e o `uvicorn` fecha essa conexão
        # específica — o processo continua vivo e atende normalmente
        # numa conexão nova, que é o que se verifica.
        with httpx.Client(base_url=f"http://127.0.0.1:{porta}", timeout=10.0) as cliente2:
            assert cliente2.get("/apartamentos/101/reservas").json() == []
    finally:
        codigo_saida, saida = parar_processo_normalmente(proc)
    assert codigo_saida == 0, f"o processo deveria continuar vivo (excepção tratada, sem queda). Saída:\n{saida}"

    conexao = persistencia.conectar(bancos["app_db"])
    try:
        conf = persistencia.obter_confirmacao(conexao, id=pendencia_id, session_id=sid)
        assert conf["estado"] == "aprovada"  # a aprovação comitou; a tool nunca executou.
        assert persistencia.listar_reservas_ativas(conexao, "101") == []

        interrompidas = confirmacao_mod.reconciliar_aprovadas_sem_execucao(conexao)
        assert interrompidas == [pendencia_id]
        assert persistencia.listar_reservas_ativas(conexao, "101") == []
    finally:
        conexao.close()
