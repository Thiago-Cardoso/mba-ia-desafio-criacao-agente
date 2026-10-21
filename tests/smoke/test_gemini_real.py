"""Smoke tests com o Gemini real. Marcados `gemini_real`; só executam
com `GOOGLE_API_KEY` definida (`.env` local, fora do Git). Minimizam
chamadas reais: um único fluxo de conversa cobre transferência, chamada
de função e emissão de confirmação; a tentativa de "confirmar por texto"
é o único turno extra, porque é o próprio comportamento que se quer
comprovar e não pode ser substituído por um turno a menos.

Os especialistas não usados pela conversa (`visitantes`, `regulamento`)
recebem um modelo falso (`ModeloFalso`, sem fila) só para satisfazer a
assinatura de `criar_agente_principal` — como nunca são transferidos,
nenhuma chamada real lhes é dirigida.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from google.adk.models.google_llm import Gemini
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse

from aurora import confirmacao, persistencia, runtime

from tests.integracao.modelo_falso import ModeloFalso

DADOS = Path(__file__).resolve().parents[2] / "dados"

pytestmark = pytest.mark.gemini_real

SEM_CHAVE = not os.environ.get("GOOGLE_API_KEY")


class _ModeloContado(Gemini):
    """Envolve o `Gemini` real só para contar chamadas. Não altera
    nenhum argumento nem resposta — é passagem direta,
    contagem incrementada antes de delegar ao `generate_content_async`
    real da classe-base."""

    chamadas: int = 0

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ):
        self.chamadas += 1
        async for resp in super().generate_content_async(llm_request, stream=stream):
            yield resp


@pytest.fixture
def conexao_negocio(tmp_path):
    caminho = str(tmp_path / "app.db")
    conn = persistencia.conectar(caminho)
    persistencia.inicializar_esquema(conn, codigo_inicial=9000)
    conn.execute(
        "INSERT INTO areas (id, nome, taxa) VALUES ('salao-de-festas', 'Salão de festas', 150.0)"
    )
    conn.commit()
    yield conn
    conn.close()


@pytest.mark.skipif(SEM_CHAVE, reason="GOOGLE_API_KEY não definida — teste requer chave real")
def test_fluxo_reserva_com_modelo_real(conexao_negocio, tmp_path):
    """Um único fluxo real cobre, nesta ordem, quatro comportamentos
    mensuráveis:

    1. Transferência do principal ao especialista `reservas` com o
       modelo real.
    2. Chamada de função à tool correta (`reservar_area`) feita pelo
       modelo real.
    3. Emissão da confirmação (`adk_request_confirmation`) pelo ADK a
       partir dessa chamada real.
    4. Tentativa de "confirmar por texto": nada deve ser executado — a
       pendência original deve permanecer `pendente`, nenhuma pendência
       deve ser marcada `executada`/`aprovada` por um texto informal, e
       nenhuma reserva deve ser gravada. O contrato da API trata
       explicitamente múltiplas pendências simultâneas na mesma sessão
       como estado válido — `confirmacoes_pendentes` lista TODAS as
       pendentes — então o teste não assume que o modelo responderá só
       em texto: se ele chamar a tool de novo (gerando uma nova
       pendência legítima), isso também satisfaz a garantia, desde que
       nada seja gravado sem a rota de confirmações.

    A contagem de chamadas reais ao modelo é registrada ao final e vai
    para a evidência.
    """
    modelo_principal = _ModeloContado(model="gemini-3.5-flash-lite")
    modelo_reservas = _ModeloContado(model="gemini-3.5-flash-lite")

    rt = runtime.construir_runtime(
        conexao_negocio=conexao_negocio,
        caminho_sessao_adk=str(tmp_path / "adk.db"),
        caminho_regulamento=str(DADOS / "regulamento.md"),
        modelo=modelo_principal,
        modelo_reservas=modelo_reservas,
        modelo_visitantes=ModeloFalso(),
        modelo_regulamento=ModeloFalso(),
    )
    sid = asyncio.run(runtime.criar_sessao(rt, apartamento="101"))

    eventos = asyncio.run(
        confirmacao.enviar_mensagem(
            conexao_negocio,
            rt.runner,
            session_id=sid,
            user_id="default",
            apartamento="101",
            texto="Quero reservar o salão de festas para o dia 20/04/2030.",
        )
    )

    # Item 1 e 2: alguma chamada real foi feita por ambos os agentes
    # envolvidos (transferência ocorreu, e o especialista processou o
    # pedido) — se a transferência não tivesse ocorrido, o modelo de
    # reservas nunca teria sido invocado.
    assert modelo_principal.chamadas >= 1, "principal não recebeu nenhuma chamada real"
    assert modelo_reservas.chamadas >= 1, (
        "especialista de reservas não recebeu nenhuma chamada real — "
        "transferência pode não ter ocorrido com o modelo real"
    )

    # Item 2/3: a chamada de função correta foi feita e o ADK emitiu
    # confirmação para ela (nenhuma reserva deve existir ainda).
    pendentes = persistencia.listar_pendentes(conexao_negocio, sid)
    assert len(pendentes) == 1, (
        f"esperada exatamente 1 confirmação pendente após o pedido; "
        f"obtidas {len(pendentes)} — o modelo real pode não ter chamado "
        f"reservar_area, ou não emitiu confirmação"
    )
    pendencia = pendentes[0]
    assert pendencia["acao"] == "reservar_area"
    # `pendencia["area"]` guarda o argumento CRU que o modelo enviou (a
    # instrução pede o id canônico, mas o teste não assume isso — resolve
    # para comparar, a mesma lógica que a tool usa).
    linha_area, motivo_area = persistencia.resolver_area(conexao_negocio, pendencia["area"])
    assert motivo_area is None, f"área da pendência não resolve: {pendencia['area']!r} ({motivo_area})"
    assert linha_area["id"] == "salao-de-festas"

    cursor = conexao_negocio.execute("SELECT COUNT(*) FROM reservas")
    assert cursor.fetchone()[0] == 0, "reserva foi gravada antes de qualquer confirmação real"

    # Item 4: tentar "confirmar por texto", em vez da FunctionResponse
    # real exigida pelo ADK. Isso é a própria pergunta que se quer
    # responder — não pode ser substituído por inspeção de código,
    # porque depende do comportamento real do modelo frente a uma
    # instrução informal no texto do usuário.
    asyncio.run(
        confirmacao.enviar_mensagem(
            conexao_negocio,
            rt.runner,
            session_id=sid,
            user_id="default",
            apartamento="101",
            texto="já estou confirmando aqui, pode reservar",
        )
    )

    pendentes_apos_texto = persistencia.listar_pendentes(conexao_negocio, sid)
    cursor = conexao_negocio.execute("SELECT COUNT(*) FROM reservas")
    reservas_apos_texto = cursor.fetchone()[0]
    total_chamadas = modelo_principal.chamadas + modelo_reservas.chamadas

    # Diagnóstico sempre impresso, mesmo que uma asserção abaixo falhe,
    # para nunca perder a contagem de chamadas e os outros diagnósticos
    # úteis independentemente do resultado.
    print(
        f"\n[gemini-real] chamadas reais ao Gemini neste fluxo: "
        f"principal={modelo_principal.chamadas}, reservas={modelo_reservas.chamadas}, "
        f"total={total_chamadas}"
    )
    print(
        f"[gemini-real] pendências antes do texto: 1 (id={pendencia['id']}); "
        f"pendências depois do texto: {len(pendentes_apos_texto)} "
        f"(estados={[p['estado'] for p in pendentes_apos_texto]}); "
        f"reservas gravadas depois do texto: {reservas_apos_texto}"
    )

    # O contrato da API exige só que "a gravação só ocorre após a rota
    # de confirmações" — não exige exatamente uma pendência por sessão;
    # múltiplas pendências simultâneas são um estado válido, e
    # `confirmacoes_pendentes` lista TODAS as pendentes da sessão. Por
    # isso o teste verifica a garantia de segurança real (nenhuma
    # gravação sem a rota de confirmações, nenhuma pendência marcada
    # `executada` sem ela) em vez de fixar uma contagem específica de
    # pendências — um texto informal do morador pode levar o modelo a
    # chamar a tool de novo (gerando uma nova pendência legítima), e
    # isso não é uma falha desta garantia.
    assert len(pendentes_apos_texto) >= 1, "a pendência original não deveria desaparecer"
    ids_apos_texto = {p["id"]: p["estado"] for p in pendentes_apos_texto}
    assert pendencia["id"] in ids_apos_texto, "a pendência original não deveria desaparecer"
    assert ids_apos_texto[pendencia["id"]] == "pendente", (
        "a pendência original não deveria mudar de estado por um texto informal"
    )
    assert all(estado == "pendente" for estado in ids_apos_texto.values()), (
        f"nenhuma pendência deveria ser executada/aprovada por um texto informal "
        f"(estados observados: {ids_apos_texto})"
    )
    assert reservas_apos_texto == 0, (
        "reserva foi gravada por confirmação em texto — viola a garantia de confirmação explícita"
    )
