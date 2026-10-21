"""Testes de `aurora.subir.main()`: provam a ordem de inicialização
(`.env` carregado e a chave do Gemini exigida antes de montar a
aplicação e subir o servidor) sem montar a aplicação real, sem abrir
porta nenhuma e sem qualquer chamada ao Gemini — todas as peças que
fariam isso (`montar_app`, `trava_exclusiva`, `uvicorn.run`) são
substituídas por dublês que só registram a ordem das chamadas.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest

from aurora import subir
from aurora.config import ChaveGeminiAusenteError


def _instalar_dubles(monkeypatch, ordem, *, exigir_chave=None):
    def dublê_carregar_env():
        ordem.append("carregar_env")

    def dublê_exigir_chave():
        ordem.append("exigir_chave")
        if exigir_chave is not None:
            raise exigir_chave

    def dublê_montar_app(**_kwargs):
        ordem.append("montar_app")
        return object(), object(), object()

    def dublê_run(_app, **_kwargs):
        ordem.append("uvicorn_run")

    @contextmanager
    def dublê_trava(_caminho):
        ordem.append("trava")
        yield object()

    monkeypatch.setattr(subir, "carregar_variaveis_de_ambiente", dublê_carregar_env)
    monkeypatch.setattr(subir, "exigir_chave_gemini", dublê_exigir_chave)
    monkeypatch.setattr(subir, "montar_app", dublê_montar_app)
    monkeypatch.setattr(subir, "trava_exclusiva", dublê_trava)
    monkeypatch.setattr(subir.uvicorn, "run", dublê_run)
    monkeypatch.setattr(
        subir,
        "caminhos_padrao",
        lambda: {
            "dir_dados": "dir-dados-fake",
            "caminho_app_db": "app-db-fake",
            "caminho_adk_db": "adk-db-fake",
            "caminho_lock": "lock-fake",
        },
    )


def test_main_carrega_env_e_exige_a_chave_antes_de_montar_a_aplicacao(monkeypatch):
    ordem: list[str] = []
    _instalar_dubles(monkeypatch, ordem)

    subir.main()

    assert ordem == ["carregar_env", "exigir_chave", "trava", "montar_app", "uvicorn_run"]


def test_main_sem_chave_para_antes_de_montar_app_e_nunca_sobe_o_servidor(monkeypatch):
    ordem: list[str] = []
    _instalar_dubles(
        monkeypatch,
        ordem,
        exigir_chave=ChaveGeminiAusenteError("GOOGLE_API_KEY não está definida."),
    )

    with pytest.raises(SystemExit):
        subir.main()

    assert ordem == ["carregar_env", "exigir_chave"]
