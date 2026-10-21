"""Arquivo de bloqueio compartilhado entre `subir` e `restaurar`: só um
dos dois roda por vez, e `restaurar` exige a API parada.

Usa `fcntl.flock` (POSIX) em vez de testar a mera existência de um
arquivo: o sistema operacional libera a trava automaticamente quando o
processo termina, inclusive por parada normal (Ctrl+C) ou queda, o que
evita um arquivo "preso" por uma subida anterior que não encerrou
corretamente — um arquivo que só existisse não teria essa garantia.
"""

from __future__ import annotations

import fcntl
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Iterator


class TravaOcupadaError(Exception):
    """A trava já está em uso por outro processo (API rodando, ou outra
    operação exclusiva em curso)."""


@contextmanager
def trava_exclusiva(caminho: str | Path) -> Iterator[IO[str]]:
    caminho = Path(caminho)
    caminho.parent.mkdir(parents=True, exist_ok=True)
    arquivo = open(caminho, "a+")
    try:
        try:
            fcntl.flock(arquivo.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise TravaOcupadaError(
                f"não foi possível obter a trava exclusiva em {caminho}"
            ) from exc
        yield arquivo
    finally:
        try:
            fcntl.flock(arquivo.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        arquivo.close()
