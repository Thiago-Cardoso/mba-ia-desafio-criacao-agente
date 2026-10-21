"""Envoltório de teste em torno de um `google.adk.runners.Runner` real,
para injetar falhas nos pontos críticos do fluxo de confirmação:
antes da execução da tool, e depois de o efeito de negócio já ter sido
gravado (com e sem queda do processo).

**Nunca é importado por `src/aurora/`.** `runner` já é um parâmetro de
`confirmacao.enviar_mensagem`/`responder_confirmacao` e de
`Runtime.runner` (dependência substituível por desenho, não uma porta
nova criada para este teste): este módulo só troca o objeto por um
decorador no processo de teste, sem tocar em nenhum arquivo de
produção.
"""

from __future__ import annotations

import os
from typing import Any


def _eh_resposta_de_confirmacao(new_message) -> bool:
    if new_message is None or not getattr(new_message, "parts", None):
        return False
    return any(getattr(p, "function_response", None) for p in new_message.parts)


class RunnerComFalhaInjetada:
    """Decora um `Runner` real. `modo` decide o ponto de falha, lido de
    `AURORA_FALHA_MODO` pelo entrypoint de teste (`app_de_teste.py`):

    - `"antes_da_tool"`: levanta uma excepção antes de delegar ao
      `Runner` real, ao retomar uma confirmação — nenhum evento é
      produzido, a tool nunca executa.
    - `"queda_apos_x1"`: encerra o processo (`os._exit`) antes de
      delegar ao `Runner` real, ao retomar uma confirmação — simula a
      queda do processo entre a transição de estado já comitada por
      quem chamou `responder_confirmacao` (antes desta chamada) e a
      execução da tool.
    - `"queda_apos_commit"`: delega normalmente ao `Runner` real,
      repassando todos os eventos reais (a tool já gravou o efeito de
      negócio durante essa delegação) e encerra o processo (`os._exit`)
      depois do último evento, antes de devolver o controle ao chamador
      — simula a queda entre a gravação do efeito e a resposta HTTP.
    - `"excecao_apos_commit"`: igual ao anterior, mas levanta uma
      excepção comum em vez de encerrar o processo — o processo
      continua vivo; é `confirmacao.responder_confirmacao` (produção)
      que decide o que fazer com essa excepção.
    - `None`/ausente: delega normalmente, sem falha nenhuma.
    """

    def __init__(self, runner_real: Any, *, modo: str | None):
        self._real = runner_real
        self._modo = modo

    def __getattr__(self, nome: str):
        return getattr(self._real, nome)

    async def run_async(self, *args, new_message=None, **kwargs):
        retomando_confirmacao = _eh_resposta_de_confirmacao(new_message)

        if retomando_confirmacao and self._modo == "antes_da_tool":
            raise RuntimeError("falha injetada: ADK falhou antes de executar a tool")

        if retomando_confirmacao and self._modo == "queda_apos_x1":
            os._exit(1)  # queda abrupta simulada; só no processo de teste.

        if retomando_confirmacao and self._modo in ("queda_apos_commit", "excecao_apos_commit"):
            async for ev in self._real.run_async(*args, new_message=new_message, **kwargs):
                yield ev
            if self._modo == "queda_apos_commit":
                os._exit(1)
            raise RuntimeError("falha injetada: excepção depois do efeito de negócio já gravado")

        async for ev in self._real.run_async(*args, new_message=new_message, **kwargs):
            yield ev
