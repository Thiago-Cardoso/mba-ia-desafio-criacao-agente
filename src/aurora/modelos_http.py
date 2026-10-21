"""Modelos de entrada e saída do contrato HTTP. Os nomes dos campos e
dos modelos seguem exatamente o contrato do enunciado — nenhum campo é
inventado nem renomeado.

A validação de tipo do Pydantic (corpo sem campo obrigatório, tipo
errado) produz `422` automaticamente pelo FastAPI, sem tratamento
adicional.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class CriarSessaoEntrada(BaseModel):
    apartamento: str


class CriarSessaoSaida(BaseModel):
    session_id: str


class MensagemEntrada(BaseModel):
    texto: str


class ConfirmacaoEntrada(BaseModel):
    id: str
    confirmado: bool


class ConfirmacaoPendente(BaseModel):
    id: str
    acao: str
    detalhes: dict[str, Any]


class RespostaMensagem(BaseModel):
    resposta: str
    confirmacoes_pendentes: list[ConfirmacaoPendente]


class ReservaSaida(BaseModel):
    codigo: str
    area: str
    data: str


class VisitanteSaida(BaseModel):
    nome: str
    data: str
