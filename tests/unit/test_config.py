"""Testes de `aurora.config`: carregamento de `.env` e verificação da
chave do Gemini antes de subir a API. Nenhum teste aqui faz qualquer
chamada real ao Gemini nem cria dados de negócio — só manipulam
variáveis de ambiente e arquivos `.env` temporários.
"""

from __future__ import annotations

import os

import pytest

from aurora import config


def test_carregar_variaveis_de_ambiente_le_arquivo_informado(tmp_path, monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    arquivo_env = tmp_path / ".env"
    arquivo_env.write_text("GOOGLE_API_KEY=chave-do-arquivo\n", encoding="utf-8")

    config.carregar_variaveis_de_ambiente(arquivo_env)

    assert os.environ["GOOGLE_API_KEY"] == "chave-do-arquivo"


def test_carregar_variaveis_de_ambiente_resolve_caminho_independente_do_cwd(tmp_path, monkeypatch):
    """O caminho padrão (`CAMINHO_ENV_PADRAO`) vem da raiz do piloto
    (`RAIZ_PILOTO`), calculada a partir de `__file__`, não do diretório
    de trabalho do processo — por isso o teste muda o `cwd` para uma
    pasta sem relação alguma com o `.env` e confirma que o arquivo é
    encontrado mesmo assim."""
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    arquivo_env = tmp_path / ".env"
    arquivo_env.write_text("GOOGLE_API_KEY=chave-de-outra-pasta\n", encoding="utf-8")
    monkeypatch.setattr(config, "CAMINHO_ENV_PADRAO", arquivo_env)

    outra_pasta = tmp_path / "pasta-qualquer" / "mais-um-nivel"
    outra_pasta.mkdir(parents=True)
    monkeypatch.chdir(outra_pasta)

    config.carregar_variaveis_de_ambiente()

    assert os.environ["GOOGLE_API_KEY"] == "chave-de-outra-pasta"


def test_variavel_ja_exportada_tem_precedencia_sobre_o_arquivo(tmp_path, monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "chave-do-ambiente")
    arquivo_env = tmp_path / ".env"
    arquivo_env.write_text("GOOGLE_API_KEY=chave-do-arquivo\n", encoding="utf-8")

    config.carregar_variaveis_de_ambiente(arquivo_env)

    assert os.environ["GOOGLE_API_KEY"] == "chave-do-ambiente"


def test_carregar_variaveis_de_ambiente_nao_falha_se_arquivo_nao_existe(tmp_path, monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setenv("GOOGLE_API_KEY", "chave-ja-exportada")
    arquivo_inexistente = tmp_path / "nao-existe" / ".env"

    config.carregar_variaveis_de_ambiente(arquivo_inexistente)

    assert os.environ["GOOGLE_API_KEY"] == "chave-ja-exportada"


def test_exigir_chave_gemini_passa_quando_chave_definida(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "qualquer-valor")
    config.exigir_chave_gemini()  # não deve levantar.


def test_exigir_chave_gemini_levanta_erro_claro_e_sem_vazar_segredo(monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    with pytest.raises(config.ChaveGeminiAusenteError) as excinfo:
        config.exigir_chave_gemini()

    mensagem = str(excinfo.value)
    assert "GOOGLE_API_KEY" in mensagem
    assert ".env" in mensagem
    # a mensagem só pode orientar como configurar — nunca conter um valor
    # de chave (não há valor nenhum definido neste teste, então a
    # garantia real é: nenhum segredo aparece quando a chave está ausente).
    assert "AIza" not in mensagem
