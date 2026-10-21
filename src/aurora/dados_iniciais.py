"""Leitura somente leitura dos arquivos JSON de `dados/`, para a carga
inicial do esquema (`apartamentos`, `areas`) e para a restauração
(`reservas`, `visitantes`).

Nunca escreve em `dados/`. Não decide nenhuma regra de negócio: só lê e
devolve os registros como estão nos arquivos.
"""

from __future__ import annotations

import json
from pathlib import Path


def _carregar(caminho: Path) -> list[dict]:
    return json.loads(caminho.read_text(encoding="utf-8"))


def carregar_apartamentos(dir_dados: str | Path) -> list[dict]:
    return _carregar(Path(dir_dados) / "apartamentos.json")


def carregar_areas(dir_dados: str | Path) -> list[dict]:
    return _carregar(Path(dir_dados) / "areas.json")


def carregar_reservas_iniciais(dir_dados: str | Path) -> list[dict]:
    return _carregar(Path(dir_dados) / "reservas.json")


def carregar_visitantes_iniciais(dir_dados: str | Path) -> list[dict]:
    return _carregar(Path(dir_dados) / "visitantes.json")


def maior_numero_de_codigo(reservas: list[dict]) -> int:
    """Maior número entre os códigos `RSV-<n>` da lista. Um código de
    reserva nunca pode repetir, mesmo o de uma reserva cancelada, então
    o contador durável (`persistencia.proximo_codigo`) deve começar
    acima deste valor."""
    maior = 0
    for reserva in reservas:
        numero = int(reserva["codigo"].rsplit("-", 1)[-1])
        maior = max(maior, numero)
    return maior
