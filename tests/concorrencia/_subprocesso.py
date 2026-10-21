"""Utilitário de teste para subir/parar a API real (reaproveita
`tests/reinicio/app_de_teste.py` sem falha nenhuma — estes testes só
precisam de um processo real para a concorrência HTTP, não de injeção
de falhas)."""

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

RAIZ_PILOTO = Path(__file__).resolve().parents[2]
APP_DE_TESTE = RAIZ_PILOTO / "tests" / "reinicio" / "app_de_teste.py"
DADOS = RAIZ_PILOTO / "dados"


def porta_livre() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def esperar_subir(porta: int, timeout: float = 15.0) -> None:
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


def escrever_roteiro(caminho: Path, roteiro: dict) -> None:
    caminho.write_text(json.dumps(roteiro), encoding="utf-8")


def subir_processo(*, porta: int, app_db: str, adk_db: str, lock: str, roteiro_arquivo: Path) -> subprocess.Popen:
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
        esperar_subir(porta)
    except TimeoutError:
        proc.kill()
        saida = proc.stdout.read() if proc.stdout else ""
        raise AssertionError(f"processo não subiu a tempo. Saída:\n{saida}")
    return proc


def parar_processo_normalmente(proc: subprocess.Popen, timeout: float = 15.0) -> tuple[int, str]:
    proc.send_signal(signal.SIGINT)
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
    saida = proc.stdout.read() if proc.stdout else ""
    return proc.returncode, saida
