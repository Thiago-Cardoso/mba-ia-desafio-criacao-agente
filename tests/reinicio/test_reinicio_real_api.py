"""Reinício real da API, com processo de verdade (`subprocess.Popen` +
`uvicorn`), servidor HTTP real e `DatabaseSessionService` real sobre os
MESMOS arquivos SQLite entre um processo e o seguinte.

O modelo usado é roteirizado, sem rede; o reinício de processo é real
(processo do sistema operacional, nunca o mesmo objeto Python nem uma
nova conexão no mesmo processo).

Não cobre a exclusividade de reserva / concorrência HTTP real entre
processos — isso está em `tests/concorrencia/`.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

RAIZ_PILOTO = Path(__file__).resolve().parents[2]
APP_DE_TESTE = Path(__file__).resolve().parent / "app_de_teste.py"
DADOS = RAIZ_PILOTO / "dados"


def _porta_livre() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _esperar_subir(porta: int, timeout: float = 15.0) -> None:
    prazo = time.monotonic() + timeout
    ultimo_erro = None
    while time.monotonic() < prazo:
        try:
            r = httpx.get(f"http://127.0.0.1:{porta}/apartamentos/000/reservas", timeout=0.5)
            if r.status_code == 200:
                return
        except httpx.TransportError as exc:
            ultimo_erro = exc
        time.sleep(0.1)
    raise TimeoutError(f"a API de teste não respondeu em {timeout}s: {ultimo_erro}")


def _escrever_roteiro(caminho: Path, roteiro: dict) -> None:
    caminho.write_text(json.dumps(roteiro), encoding="utf-8")


def _subir_processo(
    *, porta: int, app_db: str, adk_db: str, lock: str, roteiro_arquivo: Path
) -> subprocess.Popen:
    env = {
        **os.environ,
        "AURORA_APP_DB": app_db,
        "AURORA_ADK_DB": adk_db,
        "AURORA_LOCK": lock,
        "AURORA_DADOS": str(DADOS),
        "AURORA_PORTA_TESTE": str(porta),
        "AURORA_ROTEIRO": str(roteiro_arquivo),
    }
    proc = subprocess.Popen(
        [sys.executable, str(APP_DE_TESTE)],
        env=env,
        cwd=str(RAIZ_PILOTO),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        _esperar_subir(porta)
    except TimeoutError:
        proc.kill()
        saida = proc.stdout.read() if proc.stdout else ""
        raise AssertionError(f"processo não subiu a tempo. Saída:\n{saida}")
    return proc


def _parar_processo_normalmente(proc: subprocess.Popen, timeout: float = 15.0) -> tuple[int, str]:
    """Parada normal (Ctrl+C, nunca `kill -9`)."""
    proc.send_signal(signal.SIGINT)
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
    saida = proc.stdout.read() if proc.stdout else ""
    return proc.returncode, saida


@pytest.fixture
def bancos(tmp_path):
    return {
        "app_db": str(tmp_path / "app.db"),
        "adk_db": str(tmp_path / "adk.db"),
        "lock": str(tmp_path / "aurora.lock"),
        "tmp_path": tmp_path,
    }


# ---------------------------------------------------------------------------
# Reinício real com pendência em `pendente`
# ---------------------------------------------------------------------------


def test_pendencia_sobrevive_ao_reinicio_real_e_executa_uma_unica_vez(bancos):
    porta = _porta_livre()
    roteiro_a = bancos["tmp_path"] / "roteiro_a.json"
    _escrever_roteiro(
        roteiro_a,
        {
            "principal": [["transferir", "reservas"]],
            "reservas": [["chamar", "reservar_area", {"area": "salao-de-festas", "data": "2030-04-20"}]],
        },
    )

    # Processo A: cria sessão e pedido com taxa -> pendência.
    proc_a = _subir_processo(
        porta=porta, app_db=bancos["app_db"], adk_db=bancos["adk_db"], lock=bancos["lock"],
        roteiro_arquivo=roteiro_a,
    )
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{porta}", timeout=5.0) as cliente:
            sid = cliente.post("/sessoes", json={"apartamento": "101"}).json()["session_id"]
            resp = cliente.post(
                f"/sessoes/{sid}/mensagens",
                json={"texto": "Reserve o salão de festas para 2030-04-20."},
            )
            assert resp.status_code == 200
            pendencias = resp.json()["confirmacoes_pendentes"]
            assert len(pendencias) == 1
            pendencia_id = pendencias[0]["id"]
            eventos_antes = cliente.get(f"/sessoes/{sid}/eventos").json()
            quantidade_eventos_antes = len(eventos_antes)
            assert quantidade_eventos_antes > 0
            assert cliente.get("/apartamentos/101/reservas").json() == []
    finally:
        codigo_saida, saida_a = _parar_processo_normalmente(proc_a)
    assert codigo_saida == 0, f"processo A não encerrou normalmente. Saída:\n{saida_a}"

    # Processo B: mesmo banco, PROCESSO DIFERENTE do sistema operacional.
    assert proc_a.pid  # id do processo A, para registrar na evidência que B é outro PID.
    roteiro_b = bancos["tmp_path"] / "roteiro_b.json"
    _escrever_roteiro(roteiro_b, {})  # B só responde à confirmação; nenhuma mensagem de texto nova.
    proc_b = _subir_processo(
        porta=porta, app_db=bancos["app_db"], adk_db=bancos["adk_db"], lock=bancos["lock"],
        roteiro_arquivo=roteiro_b,
    )
    try:
        assert proc_b.pid != proc_a.pid  # evidência de que é um processo OS novo, não o mesmo.
        with httpx.Client(base_url=f"http://127.0.0.1:{porta}", timeout=5.0) as cliente:
            # Os eventos de antes do reinício continuam disponíveis.
            eventos_depois = cliente.get(f"/sessoes/{sid}/eventos").json()
            assert len(eventos_depois) == quantidade_eventos_antes

            # A pendência criada no processo A ainda é aprovável no processo B.
            resp = cliente.post(
                f"/sessoes/{sid}/confirmacoes", json={"id": pendencia_id, "confirmado": True}
            )
            assert resp.status_code == 200
            ativas = cliente.get("/apartamentos/101/reservas").json()
            assert ativas == [{"codigo": "RSV-4822", "area": "salao-de-festas", "data": "2030-04-20"}]

            # Execução única: reenviar a mesma aprovação recebe 409, sem duplicar.
            resp_reenvio = cliente.post(
                f"/sessoes/{sid}/confirmacoes", json={"id": pendencia_id, "confirmado": True}
            )
            assert resp_reenvio.status_code == 409
            assert cliente.get("/apartamentos/101/reservas").json() == ativas
    finally:
        codigo_saida_b, saida_b = _parar_processo_normalmente(proc_b)
    assert codigo_saida_b == 0, f"processo B não encerrou normalmente. Saída:\n{saida_b}"


# ---------------------------------------------------------------------------
# Reinício real depois de aprovação: `executada` preservada
# ---------------------------------------------------------------------------


def test_aprovacao_executada_sobrevive_ao_reinicio_e_reenvio_recebe_409(bancos):
    porta = _porta_livre()
    roteiro_a = bancos["tmp_path"] / "roteiro_a.json"
    _escrever_roteiro(
        roteiro_a,
        {
            "principal": [["transferir", "reservas"]],
            "reservas": [["chamar", "reservar_area", {"area": "salao-de-festas", "data": "2030-04-20"}]],
        },
    )

    proc_a = _subir_processo(
        porta=porta, app_db=bancos["app_db"], adk_db=bancos["adk_db"], lock=bancos["lock"],
        roteiro_arquivo=roteiro_a,
    )
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{porta}", timeout=5.0) as cliente:
            sid = cliente.post("/sessoes", json={"apartamento": "101"}).json()["session_id"]
            resp = cliente.post(
                f"/sessoes/{sid}/mensagens",
                json={"texto": "Reserve o salão de festas para 2030-04-20."},
            )
            pendencia_id = resp.json()["confirmacoes_pendentes"][0]["id"]
            # Aprova e executa ANTES do reinício.
            resp_aprov = cliente.post(
                f"/sessoes/{sid}/confirmacoes", json={"id": pendencia_id, "confirmado": True}
            )
            assert resp_aprov.status_code == 200
            ativas_antes = cliente.get("/apartamentos/101/reservas").json()
            assert len(ativas_antes) == 1
    finally:
        codigo_saida, saida_a = _parar_processo_normalmente(proc_a)
    assert codigo_saida == 0, f"processo A não encerrou normalmente. Saída:\n{saida_a}"

    roteiro_b = bancos["tmp_path"] / "roteiro_b.json"
    _escrever_roteiro(roteiro_b, {})
    proc_b = _subir_processo(
        porta=porta, app_db=bancos["app_db"], adk_db=bancos["adk_db"], lock=bancos["lock"],
        roteiro_arquivo=roteiro_b,
    )
    try:
        assert proc_b.pid != proc_a.pid
        with httpx.Client(base_url=f"http://127.0.0.1:{porta}", timeout=5.0) as cliente:
            # Reenvio da mesma aprovação, agora num processo novo: 409, sem nova gravação.
            resp_reenvio = cliente.post(
                f"/sessoes/{sid}/confirmacoes", json={"id": pendencia_id, "confirmado": True}
            )
            assert resp_reenvio.status_code == 409
            ativas_depois = cliente.get("/apartamentos/101/reservas").json()
            assert ativas_depois == ativas_antes  # exatamente a mesma reserva, nenhuma duplicata.
    finally:
        codigo_saida_b, saida_b = _parar_processo_normalmente(proc_b)
    assert codigo_saida_b == 0, f"processo B não encerrou normalmente. Saída:\n{saida_b}"


# ---------------------------------------------------------------------------
# No nível do processo real: restaurar recusa enquanto a API sobe.
# ---------------------------------------------------------------------------


def test_restaurar_recusa_enquanto_processo_real_da_api_esta_rodando(bancos):
    from aurora.trava import TravaOcupadaError, trava_exclusiva

    porta = _porta_livre()
    roteiro = bancos["tmp_path"] / "roteiro.json"
    _escrever_roteiro(roteiro, {})
    proc = _subir_processo(
        porta=porta, app_db=bancos["app_db"], adk_db=bancos["adk_db"], lock=bancos["lock"],
        roteiro_arquivo=roteiro,
    )
    try:
        with pytest.raises(TravaOcupadaError):
            with trava_exclusiva(bancos["lock"]):
                pytest.fail("não deveria obter a trava com o processo real da API rodando")
    finally:
        _parar_processo_normalmente(proc)
