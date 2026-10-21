"""Concorrência HTTP real contra um processo real da API, disparando
as duas requisições por `asyncio.gather` (nunca sequenciais, nunca com
`time.sleep` como prova).

O modelo usado é roteirizado; o processo e a concorrência de rede são
reais (duas conexões HTTP reais, mesmo processo `uvicorn`, mesmo
`app.db`).
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import httpx
import pytest

from ._subprocesso import (
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


async def _post_com_tempos(cliente: httpx.AsyncClient, url: str, corpo: dict) -> dict:
    inicio = time.monotonic()
    resp = await cliente.post(url, json=corpo)
    fim = time.monotonic()
    return {"status": resp.status_code, "corpo": resp.json(), "inicio": inicio, "fim": fim}


def _houve_sobreposicao(a: dict, b: dict) -> bool:
    """Prova de concorrência genuína: as janelas de tempo das duas
    requisições se sobrepõem (uma começou antes da outra terminar) —
    não é possível com chamadas sequenciais nem com `time.sleep` para
    forçar ordem."""
    return a["inicio"] < b["fim"] and b["inicio"] < a["fim"]


# ---------------------------------------------------------------------------
# Duas aprovações simultâneas do MESMO `id`
# ---------------------------------------------------------------------------


def test_duas_aprovacoes_simultaneas_do_mesmo_id_uma_200_uma_409(bancos):
    porta = porta_livre()
    roteiro = bancos["tmp_path"] / "roteiro.json"
    escrever_roteiro(
        roteiro,
        {
            "principal": [["transferir", "reservas"]],
            "reservas": [["chamar", "reservar_area", {"area": "salao-de-festas", "data": "2030-04-20"}]],
        },
    )
    proc = subir_processo(
        porta=porta, app_db=bancos["app_db"], adk_db=bancos["adk_db"], lock=bancos["lock"],
        roteiro_arquivo=roteiro,
    )
    try:

        async def _fluxo():
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{porta}", timeout=10.0) as cliente:
                sid = (await cliente.post("/sessoes", json={"apartamento": "101"})).json()["session_id"]
                resp = await cliente.post(
                    f"/sessoes/{sid}/mensagens",
                    json={"texto": "Reserve o salão de festas para 2030-04-20."},
                )
                pendencia_id = resp.json()["confirmacoes_pendentes"][0]["id"]

                url = f"/sessoes/{sid}/confirmacoes"
                corpo = {"id": pendencia_id, "confirmado": True}
                resultado_a, resultado_b = await asyncio.gather(
                    _post_com_tempos(cliente, url, corpo),
                    _post_com_tempos(cliente, url, corpo),
                )
                reservas_finais = (await cliente.get("/apartamentos/101/reservas")).json()
                return resultado_a, resultado_b, reservas_finais

        resultado_a, resultado_b, reservas_finais = asyncio.run(_fluxo())
    finally:
        codigo_saida, saida = parar_processo_normalmente(proc)
    assert codigo_saida == 0, f"processo não encerrou normalmente. Saída:\n{saida}"

    assert _houve_sobreposicao(resultado_a, resultado_b), (
        f"as duas requisições não se sobrepuseram no tempo: {resultado_a}, {resultado_b}"
    )
    status = sorted([resultado_a["status"], resultado_b["status"]])
    assert status == [200, 409], f"esperava uma 200 e uma 409, obtive {status}"
    assert 500 not in status
    assert len(reservas_finais) == 1
    assert reservas_finais[0] == {"codigo": "RSV-4822", "area": "salao-de-festas", "data": "2030-04-20"}


# ---------------------------------------------------------------------------
# Duas aprovações simultâneas de `id`s diferentes, sessões diferentes,
# mesma área e data (o cenário crítico de exclusividade de reserva)
# ---------------------------------------------------------------------------


def test_duas_aprovacoes_simultaneas_ids_diferentes_sessoes_diferentes_uma_reserva(bancos):
    porta = porta_livre()
    roteiro = bancos["tmp_path"] / "roteiro.json"
    escrever_roteiro(
        roteiro,
        {
            "principal": [["transferir", "reservas"], ["transferir", "reservas"]],
            "reservas": [
                ["chamar", "reservar_area", {"area": "salao-de-festas", "data": "2030-05-11"}],
                ["chamar", "reservar_area", {"area": "salao-de-festas", "data": "2030-05-11"}],
            ],
        },
    )
    proc = subir_processo(
        porta=porta, app_db=bancos["app_db"], adk_db=bancos["adk_db"], lock=bancos["lock"],
        roteiro_arquivo=roteiro,
    )
    try:

        async def _fluxo():
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{porta}", timeout=10.0) as cliente:
                sid_101 = (await cliente.post("/sessoes", json={"apartamento": "101"})).json()["session_id"]
                sid_201 = (await cliente.post("/sessoes", json={"apartamento": "201"})).json()["session_id"]

                resp_101 = await cliente.post(
                    f"/sessoes/{sid_101}/mensagens",
                    json={"texto": "Reserve o salão de festas para 2030-05-11."},
                )
                resp_201 = await cliente.post(
                    f"/sessoes/{sid_201}/mensagens",
                    json={"texto": "Reserve o salão de festas para 2030-05-11."},
                )
                pendencia_101 = resp_101.json()["confirmacoes_pendentes"][0]["id"]
                pendencia_201 = resp_201.json()["confirmacoes_pendentes"][0]["id"]

                resultado_101, resultado_201 = await asyncio.gather(
                    _post_com_tempos(
                        cliente, f"/sessoes/{sid_101}/confirmacoes",
                        {"id": pendencia_101, "confirmado": True},
                    ),
                    _post_com_tempos(
                        cliente, f"/sessoes/{sid_201}/confirmacoes",
                        {"id": pendencia_201, "confirmado": True},
                    ),
                )
                reservas_101 = (await cliente.get("/apartamentos/101/reservas")).json()
                reservas_201 = (await cliente.get("/apartamentos/201/reservas")).json()
                return resultado_101, resultado_201, reservas_101, reservas_201

        resultado_101, resultado_201, reservas_101, reservas_201 = asyncio.run(_fluxo())
    finally:
        codigo_saida, saida = parar_processo_normalmente(proc)
    assert codigo_saida == 0, f"processo não encerrou normalmente. Saída:\n{saida}"

    assert _houve_sobreposicao(resultado_101, resultado_201), (
        f"as duas requisições não se sobrepuseram no tempo: {resultado_101}, {resultado_201}"
    )
    # As duas respostas de aprovação são 200 (sem erro de servidor), mesmo
    # a que perde a disputa — nunca 409 aqui, porque os `id`s são válidos
    # e distintos; a recusa é de negócio (data_ocupada), não de estado de
    # confirmação.
    assert resultado_101["status"] == 200
    assert resultado_201["status"] == 200

    # No instante da gravação, exatamente uma reserva do salão em
    # 2030-05-11 existe, somando os dois apartamentos — garantida pelo
    # índice único parcial do SQLite, não por uma conferência feita antes
    # da gravação.
    todas_salao_0511 = [r for r in reservas_101 + reservas_201 if r["data"] == "2030-05-11"]
    assert len(todas_salao_0511) == 1

    # A resposta da aprovação recusada não revela o código da reserva
    # vencedora nem dados do outro apartamento.
    corpos = [resultado_101["corpo"], resultado_201["corpo"]]
    codigo_vencedor = todas_salao_0511[0]["codigo"]
    vencedora = next(c for c in corpos if codigo_vencedor in str(c))
    perdedora = next(c for c in corpos if c is not vencedora)
    assert codigo_vencedor not in str(perdedora)
