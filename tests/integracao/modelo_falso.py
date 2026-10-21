"""Modelo `BaseLlm` roteirizado para testes de integração sem rede.

Cada agente recebe sua própria instância, com sua própria fila de ações
(`fila`), adaptada às tools reais deste projeto.
"""

from __future__ import annotations

import json
from typing import Any

from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types
from pydantic import Field


def _texto(t: str) -> LlmResponse:
    return LlmResponse(content=types.Content(role="model", parts=[types.Part(text=t)]))


def _chamada(nome: str, args: dict) -> LlmResponse:
    return LlmResponse(
        content=types.Content(
            role="model", parts=[types.Part(function_call=types.FunctionCall(name=nome, args=args))]
        )
    )


class ModeloFalso(BaseLlm):
    """`BaseLlm` roteirizado. `fila` é uma lista de ações, consumida em
    ordem; cada ação é uma tupla:

    - `("texto", "...")`: responde com texto fixo.
    - `("chamar", "nome_da_tool", {args})`: chama uma tool/transferência.
    - `("transferir", "nome_do_agente")`: atalho para
      `("chamar", "transfer_to_agent", {"agent_name": nome})`.

    Quando a fila esvazia e a última mensagem contém uma
    `function_response` (resultado de tool, ou confirmação), responde com
    um texto que ecoa o nome da função — suficiente quando o teste não
    precisa de texto natural.
    """

    model: str = "fake"
    fila: list = Field(default_factory=list)

    async def generate_content_async(self, llm_request: LlmRequest, stream: bool = False):
        last = llm_request.contents[-1] if llm_request.contents else None
        frs = [pt.function_response for pt in (last.parts or []) if pt.function_response] if last else []

        if self.fila:
            tipo, *resto = self.fila.pop(0)
            if tipo == "texto":
                yield _texto(resto[0])
                return
            if tipo == "chamar":
                yield _chamada(resto[0], resto[1])
                return
            if tipo == "transferir":
                yield _chamada("transfer_to_agent", {"agent_name": resto[0]})
                return
            raise ValueError(f"ação desconhecida no roteiro: {tipo}")

        if frs:
            fr = frs[0]
            yield _texto(f"ok:{fr.name}:{json.dumps(fr.response, ensure_ascii=False, default=str)}")
            return

        yield _texto("(roteiro esgotado)")
