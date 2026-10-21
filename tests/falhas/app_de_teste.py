"""Processo real de teste para injeção de falhas no fluxo de
confirmação.

Monta a MESMA aplicação de produção (`aurora.subir.montar_app`), com um
modelo falso roteirizado (sem rede) e com `rt.runner` substituído por
`RunnerComFalhaInjetada` (só neste arquivo).

**Isolamento, verificado por teste** (`test_isolamento.py`):
- `src/aurora/subir.py` (entrypoint de produção) nunca importa este
  arquivo nem `envoltorio_runner.py`.
- Nenhuma rota, cabeçalho, parâmetro ou variável de ambiente do código
  de produção ativa qualquer modo de falha — `AURORA_FALHA_MODO` só é
  lido abaixo, neste arquivo de teste.
- `src/` não contém nenhum ramo de código condicionado a falha
  injetada (busca estática).

Variáveis de ambiente (além das já usadas por `tests/reinicio/app_de_teste.py`):

- `AURORA_FALHA_MODO`: um dos modos de `envoltorio_runner.RunnerComFalhaInjetada`
  (`antes_da_tool`, `queda_apos_x1`, `queda_apos_commit`, `excecao_apos_commit`).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import uvicorn

RAIZ_PILOTO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(RAIZ_PILOTO))

from aurora import subir  # noqa: E402
from aurora.trava import trava_exclusiva  # noqa: E402
from tests.falhas.envoltorio_runner import RunnerComFalhaInjetada  # noqa: E402
from tests.integracao.modelo_falso import ModeloFalso  # noqa: E402


def _fila(lista: list) -> list[tuple]:
    return [tuple(item) for item in lista]


def main() -> None:
    roteiro = json.loads(Path(os.environ["AURORA_ROTEIRO"]).read_text(encoding="utf-8"))
    modelo_principal = ModeloFalso(fila=_fila(roteiro.get("principal", [])))
    modelo_reservas = ModeloFalso(fila=_fila(roteiro.get("reservas", [])))
    modelo_visitantes = ModeloFalso(fila=_fila(roteiro.get("visitantes", [])))
    modelo_regulamento = ModeloFalso(fila=_fila(roteiro.get("regulamento", [])))

    dir_dados = os.environ.get("AURORA_DADOS", str(RAIZ_PILOTO / "dados"))
    caminho_app_db = os.environ["AURORA_APP_DB"]
    caminho_adk_db = os.environ["AURORA_ADK_DB"]
    caminho_lock = os.environ.get("AURORA_LOCK", caminho_app_db + ".lock")
    porta = int(os.environ["AURORA_PORTA_TESTE"])
    modo_falha = os.environ.get("AURORA_FALHA_MODO")

    with trava_exclusiva(caminho_lock):
        _, rt, app = subir.montar_app(
            dir_dados=dir_dados,
            caminho_app_db=caminho_app_db,
            caminho_adk_db=caminho_adk_db,
            modelo=modelo_principal,
            modelo_reservas=modelo_reservas,
            modelo_visitantes=modelo_visitantes,
            modelo_regulamento=modelo_regulamento,
        )
        if modo_falha:
            rt.runner = RunnerComFalhaInjetada(rt.runner, modo=modo_falha)
        uvicorn.run(app, host="127.0.0.1", port=porta)


if __name__ == "__main__":
    main()
