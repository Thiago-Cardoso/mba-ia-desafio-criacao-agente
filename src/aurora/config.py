"""Caminhos e variáveis de ambiente compartilhados pelos comandos
`subir` e `restaurar`.

A maioria dos caminhos aqui não é lida de `.env` diretamente — é
decisão do processo que chama `main()` (variáveis de ambiente do
sistema, já carregadas por quem invocou o comando). Este módulo só
decide os nomes das variáveis e os valores padrão. A exceção é
`carregar_variaveis_de_ambiente`, abaixo: `subir.main()` chama essa
função antes de qualquer outra coisa, justamente para que `.env` já
esteja no ambiente do processo quando os valores padrão abaixo (e,
mais à frente, o cliente do Gemini) forem lidos.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

RAIZ_PILOTO = Path(__file__).resolve().parents[2]
DADOS_PADRAO = RAIZ_PILOTO / "dados"
DIR_DATA_PADRAO = RAIZ_PILOTO / "data"
CAMINHO_ENV_PADRAO = RAIZ_PILOTO / ".env"


class ChaveGeminiAusenteError(Exception):
    """`GOOGLE_API_KEY` não está definida nem no ambiente do processo nem
    em `.env`."""


def carregar_variaveis_de_ambiente(caminho_env: str | Path | None = None) -> None:
    """Carrega `.env` a partir da raiz do piloto (`RAIZ_PILOTO/.env`),
    nunca do diretório de trabalho do processo — o comando funciona
    independentemente da pasta de onde é chamado. `override=False`
    (padrão do `python-dotenv`): uma variável já exportada no ambiente
    nunca é sobrescrita pelo valor do arquivo. Não falha se o arquivo
    não existir (ambientes que já exportam as variáveis diretamente)."""
    load_dotenv(dotenv_path=caminho_env or CAMINHO_ENV_PADRAO, override=False)


def exigir_chave_gemini() -> None:
    """Falha de forma explícita e segura, antes de subir o servidor, se
    `GOOGLE_API_KEY` não estiver definida — em vez de deixar o erro
    aparecer só na primeira mensagem ao modelo real, como um
    `ValueError` genérico vindo de dentro do cliente do Gemini. Nunca
    inclui o valor da variável na mensagem."""
    if not os.environ.get("GOOGLE_API_KEY"):
        raise ChaveGeminiAusenteError(
            "GOOGLE_API_KEY não está definida. Configure-a em "
            f"{CAMINHO_ENV_PADRAO} (copie de .env.example) ou exporte-a "
            "no ambiente antes de subir a API."
        )


def caminho_env(nome: str, padrao: Path | str) -> str:
    return os.environ.get(nome, str(padrao))


def caminhos_padrao() -> dict[str, str]:
    """Resolve os caminhos de `subir`/`restaurar` a partir do ambiente,
    com os padrões de produção (`<piloto>/data/...`). Os testes passam
    seus próprios caminhos (diretórios temporários) pelas mesmas
    variáveis de ambiente, nunca alterando este módulo."""
    dir_dados = caminho_env("AURORA_DADOS", DADOS_PADRAO)
    dir_data = Path(caminho_env("AURORA_DATA_DIR", DIR_DATA_PADRAO))
    return {
        "dir_dados": dir_dados,
        "dir_data": str(dir_data),
        "caminho_app_db": caminho_env("AURORA_APP_DB", dir_data / "app.db"),
        "caminho_adk_db": caminho_env("AURORA_ADK_DB", dir_data / "adk.db"),
        "caminho_lock": caminho_env("AURORA_LOCK", dir_data / "aurora.lock"),
    }
