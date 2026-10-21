"""Comando de restauração. Restaura só `reservas` e `visitantes` a
partir de `dados/`, numa única transação. Recusa sem alterar nada se a
API estiver rodando (mesma trava de `subir.py`) ou se houver
confirmação `aprovada` sem execução (operação de negócio incompleta).

Isto não é um reset destrutivo: sessões, eventos do ADK, confirmações e
o contador de códigos nunca são tocados aqui.
"""

from __future__ import annotations

import logging
from pathlib import Path

from . import dados_iniciais, persistencia
from .config import caminhos_padrao
from .subir import calcular_codigo_inicial
from .trava import TravaOcupadaError, trava_exclusiva

_log = logging.getLogger(__name__)


class RestauracaoRecusadaError(Exception):
    """Recusa sem alterar nada: confirmação `aprovada` sem execução
    (operação de negócio incompleta)."""


def restaurar(*, dir_dados: str, caminho_app_db: str) -> dict:
    """Executa a restauração. O chamador (`main()` ou o teste) é
    responsável por já estar dentro da trava exclusiva (`trava_exclusiva`)
    — esta função não a abre, para que `main()` possa decidir em que
    ponto exato a trava é obtida e relatar o erro de forma específica.

    **Clone limpo.** `restaurar` não pode presumir que `subir` já criou
    o esquema (a tabela `confirmacoes`), porque os dois comandos existem
    separadamente, sem ordem obrigatória entre eles — `restaurar` pode
    ser o primeiro a rodar num clone limpo. Por isso esta função garante
    o diretório e chama `inicializar_esquema` — a mesma função
    idempotente que `subir` já chama em toda subida (`CREATE TABLE IF
    NOT EXISTS`, `INSERT ... ON CONFLICT DO NOTHING`) — antes de
    qualquer leitura. Isso não cria nenhuma tabela nova, não muda
    nenhuma transição de estado e não altera o comportamento quando o
    esquema já existe.
    """
    Path(caminho_app_db).parent.mkdir(parents=True, exist_ok=True)
    conexao = persistencia.conectar(caminho_app_db)
    persistencia.inicializar_esquema(conexao, codigo_inicial=calcular_codigo_inicial(dir_dados))
    try:
        aprovadas = persistencia.listar_aprovadas_sem_execucao(conexao)
        if aprovadas:
            raise RestauracaoRecusadaError(
                f"restauração recusada: {len(aprovadas)} confirmação(ões) 'aprovada' sem "
                "execução (operação incompleta); nada foi alterado"
            )
        pendentes = conexao.execute(
            "SELECT COUNT(*) AS n FROM confirmacoes WHERE estado = 'pendente'"
        ).fetchone()["n"]
        resultado = persistencia.restaurar_reservas_e_visitantes(
            conexao,
            reservas=dados_iniciais.carregar_reservas_iniciais(dir_dados),
            visitantes=dados_iniciais.carregar_visitantes_iniciais(dir_dados),
        )
        resultado["confirmacoes_pendentes_relatadas"] = pendentes
        return resultado
    finally:
        conexao.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    caminhos = caminhos_padrao()

    try:
        with trava_exclusiva(caminhos["caminho_lock"]):
            resultado = restaurar(
                dir_dados=caminhos["dir_dados"], caminho_app_db=caminhos["caminho_app_db"]
            )
    except TravaOcupadaError as exc:
        _log.error("restauração recusada: a API parece estar em execução (%s)", exc)
        raise SystemExit(1) from exc
    except RestauracaoRecusadaError as exc:
        _log.error(str(exc))
        raise SystemExit(1) from exc

    _log.info(
        "restauração concluída: %d reserva(s) removida(s), %d recriada(s); "
        "%d visitante(s) removido(s), %d recriado(s); %d confirmação(ões) pendente(s) "
        "relatada(s) (não alteradas).",
        resultado["reservas_removidas"],
        resultado["reservas_recriadas"],
        resultado["visitantes_removidos"],
        resultado["visitantes_recriados"],
        resultado["confirmacoes_pendentes_relatadas"],
    )


if __name__ == "__main__":
    main()
