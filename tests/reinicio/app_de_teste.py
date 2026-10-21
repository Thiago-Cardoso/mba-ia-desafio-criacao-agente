"""Processo real de teste para o reinício real da API.

Monta a MESMA aplicação de produção (`aurora.subir.montar_app`), com os
mesmos módulos de runtime, API, persistência e confirmação — a única
diferença em relação a `aurora.subir.main()` é o modelo: aqui é o
`BaseLlm` roteirizado de teste, porque o objetivo é provar persistência
através de um reinício real de PROCESSO (`DatabaseSessionService` sobre
SQLite real, servidor HTTP real), não o comportamento do modelo.

Este arquivo nunca é importado pelo código de produção (`src/aurora/`);
só por este script e pelos testes de reinício real
(`test_reinicio_real_api.py`), que o invocam como um subprocesso de
verdade (`sys.executable app_de_teste.py`), nunca dentro do processo de
teste.

Variáveis de ambiente (todas obrigatórias, fornecidas pelo teste):

- `AURORA_APP_DB`, `AURORA_ADK_DB`: caminhos dos bancos SQLite (os
  mesmos entre um processo e o seguinte, para provar a persistência).
- `AURORA_PORTA_TESTE`: porta HTTP.
- `AURORA_ROTEIRO`: caminho de um arquivo JSON com as filas do modelo
  falso por agente (`{"principal": [...], "reservas": [...], ...}`),
  cada ação como lista `["chamar", "nome", {...}]`/`["transferir",
  "nome"]`/`["texto", "..."]` (ver `tests/integracao/modelo_falso.py`).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import uvicorn

RAIZ_PILOTO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(RAIZ_PILOTO))  # para importar `tests.integracao.modelo_falso` como script solto

from aurora import subir  # noqa: E402
from aurora.trava import trava_exclusiva  # noqa: E402
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

    with trava_exclusiva(caminho_lock):
        _, _, app = subir.montar_app(
            dir_dados=dir_dados,
            caminho_app_db=caminho_app_db,
            caminho_adk_db=caminho_adk_db,
            modelo=modelo_principal,
            modelo_reservas=modelo_reservas,
            modelo_visitantes=modelo_visitantes,
            modelo_regulamento=modelo_regulamento,
        )
        uvicorn.run(app, host="127.0.0.1", port=porta)


if __name__ == "__main__":
    main()
