"""Tools de negócio para os especialistas de reservas e visitantes.

Cada tool recebe a identidade do apartamento de `tool_context.state`,
nunca como parâmetro visível ao modelo. Nenhuma tool aceita um
apartamento escolhido pelo modelo. O acesso ao banco de negócio é
injetado por fechamento (closure) na fábrica de cada agente
(`agentes.py`), não como parâmetro visível ao modelo.

**Assinatura do callable de `require_confirmation`.** Lido em
`google.adk.tools.function_tool.FunctionTool` (ADK 2.12.0,
`_prepare_invocation_args` e `_invoke_callable`): o callable é chamado
como `target(**args_to_call)`, em que `args_to_call` são os **mesmos
parâmetros da assinatura da própria tool** (filtrados pela assinatura
da tool, não pela do callable), incluindo `tool_context` se a tool o
declarar. Por isso o callable de `reservar_area` precisa aceitar
exatamente `area`, `data` e `tool_context` — a mesma assinatura da tool.
Pode ser síncrono ou assíncrono (`_invoke_callable` trata os dois casos).

**Composição com o registro de confirmações.** Quando uma tool com
`require_confirmation` é de fato invocada (ou seja, o ADK já considera a
confirmação satisfeita), o corpo da função localiza a linha `aprovada`
correspondente por `tool_context.function_call_id` (que é o id ORIGINAL
da chamada, não o da confirmação) e grava a reserva/autorização na MESMA
transação que marca a confirmação como `executada`. Essa composição
depende da camada que observa os eventos do ADK e cria as linhas
`pendente` (`confirmacao.sincronizar_pendencias`) ter sido chamada antes
— nunca pelas próprias tools. Se a tool for reexecutada pelo ADK ("at
least once") depois de já ter gravado o efeito, ela devolve o
`resultado` já salvo, sem gravar de novo (idempotência).

**Validação do argumento `area`.** Com o modelo real
(`gemini-3.5-flash-lite`), o argumento `area` pode chegar como o nome
legível em português ("salão de festas"), não o id canônico
("salao-de-festas") — argumentos gerados pelo modelo são entrada não
confiável e precisam ser validados antes de qualquer decisão de negócio.
Resolver `area` literalmente contra `areas.id` e assumir `taxa = 0`
quando não encontra tratava qualquer área não reconhecida como
**gratuita**, pulando a confirmação devida a uma reserva com cobrança e
gravando a reserva com uma string de área nunca validada. Por isso
`area` é resolvida contra a tabela oficial (`persistencia.resolver_area`,
por id OU por nome legível com correspondência EXATA e normalizada —
nunca parcial/fuzzy) antes de decidir taxa ou tocar em qualquer dado, e
uma área não resolvida nunca é classificada como gratuita.
"""

from __future__ import annotations

import sqlite3

from google.adk.tools import FunctionTool, ToolContext

from . import persistencia
from .regulamento import Regulamento


_ERRO_AREA = {
    "area_desconhecida": "área desconhecida: não corresponde a nenhum id ou nome oficial",
    "area_ambigua": "área ambígua: corresponde a mais de uma área oficial",
}


def _erro_area(motivo: str) -> dict:
    return {"status": "erro", "motivo": motivo, "mensagem": _ERRO_AREA[motivo]}


def _area_tem_taxa(conexao: sqlite3.Connection, area: str, data: str, tool_context: ToolContext) -> bool:
    """Decide se `reservar_area` precisa de confirmação: só quando a
    área — resolvida pelo id oficial ou, em último caso, pelo nome legível
    com correspondência exata (`persistencia.resolver_area`) — tem taxa
    maior que zero. Área não resolvida (desconhecida ou ambígua) devolve
    `False`, nunca `True` por engano: isso NÃO autoriza a reserva direto
    como se fosse gratuita — o corpo de `reservar_area` resolve a área de
    novo e rejeita explicitamente antes de gravar qualquer coisa, sem
    nunca criar ou alterar uma confirmação para uma área inválida."""
    linha, _motivo = persistencia.resolver_area(conexao, area)
    if linha is None:
        return False
    return bool(linha["taxa"] and linha["taxa"] > 0)


def criar_tools_reservas(conexao: sqlite3.Connection) -> list[FunctionTool]:
    """Fábrica das tools do especialista de reservas, com o banco de
    negócio injetado por fechamento."""

    def verificar_disponibilidade(area: str, data: str, tool_context: ToolContext) -> dict:
        """Verifica se uma área comum do condomínio está livre numa data.

        Devolve só se a área está livre ou ocupada, nunca de quem é a
        reserva existente.

        Args:
            area: identificador da área comum (por exemplo "quadra") ou,
                se o id exato não for conhecido, o nome legível tal como
                aparece para o morador (por exemplo "Salão de festas").
            data: data da reserva, no formato AAAA-MM-DD.
        """
        linha, motivo = persistencia.resolver_area(conexao, area)
        if linha is None:
            return _erro_area(motivo)
        disponivel = persistencia.area_disponivel(conexao, area=linha["id"], data=data)
        return {"status": "ok", "disponivel": disponivel}

    def require_confirmation_reservar_area(area: str, data: str, tool_context: ToolContext) -> bool:
        return _area_tem_taxa(conexao, area, data, tool_context)

    def reservar_area(area: str, data: str, tool_context: ToolContext) -> dict:
        """Reserva uma área comum do condomínio para o apartamento da sessão.

        Áreas com taxa geram cobrança e exigem confirmação do morador pela
        rota de confirmações antes de a reserva ser gravada; áreas sem
        taxa são gravadas direto, sem confirmação.

        Args:
            area: identificador da área comum (id canônico, de
                preferência) ou o nome legível tal como aparece para o
                morador, se o id exato não for conhecido.
            data: data da reserva, no formato AAAA-MM-DD.
        """
        linha, motivo = persistencia.resolver_area(conexao, area)
        if linha is None:
            # Nunca interpreta área não resolvida como gratuita: nenhuma
            # reserva é criada, nenhum estado de confirmação é tocado —
            # `require_confirmation_reservar_area` já devolveu `False`
            # para esta mesma área (ver `_area_tem_taxa`), então o ADK
            # chamou esta função direto, sem nenhuma pendência envolvida.
            return _erro_area(motivo)

        apartamento = tool_context.state["apartamento"]
        if linha["taxa"] and linha["taxa"] > 0:
            return _executar_reserva_confirmada(
                area=linha["id"], data=data, apartamento=apartamento, tool_context=tool_context
            )
        codigo = persistencia.proximo_codigo(conexao)
        with persistencia.transacao(conexao):
            try:
                persistencia.inserir_reserva(
                    conexao, codigo=codigo, apartamento=apartamento, area=linha["id"], data=data
                )
            except sqlite3.IntegrityError:
                return {"status": "ok", "reservado": False, "motivo": "data_ocupada"}
        return {"status": "ok", "reservado": True, "codigo": codigo, "area": linha["id"]}

    def _executar_reserva_confirmada(
        *, area: str, data: str, apartamento: str, tool_context: ToolContext
    ) -> dict:
        """Executa `reservar_area` depois de a confirmação ter sido
        concedida. Localiza a linha `aprovada` pelo `function_call_id`
        **original** da chamada — não o da confirmação — e grava a
        reserva na mesma transação que marca a confirmação `executada`.
        Se a área já estiver ocupada, a transação inteira é desfeita e
        uma segunda transação marca `falhou` com o motivo.

        `area` já chega resolvida ao id canônico pelo chamador
        (`reservar_area`, que só delega aqui depois de confirmar que a
        área é paga e válida); esta função nunca resolve de novo.
        """
        original_fc_id = tool_context.function_call_id
        conf = persistencia.obter_confirmacao_por_function_call_id(
            conexao, original_function_call_id=original_fc_id
        )
        if conf is None:
            raise RuntimeError(
                f"nenhuma confirmação encontrada para function_call_id={original_fc_id!r}; "
                "a pendência deveria ter sido criada por confirmacao.sincronizar_pendencias "
                "antes da aprovação"
            )
        if conf["estado"] == "executada":
            # o ADK reexecutou a tool ("at least once"); devolve o mesmo
            # resultado já gravado, sem gravar de novo (idempotência).
            return conf["resultado"]
        if conf["estado"] != "aprovada":
            raise RuntimeError(
                f"reservar_area reexecutada com confirmação em estado inesperado: "
                f"{conf['estado']!r} (esperava 'aprovada' ou 'executada')"
            )

        codigo = persistencia.proximo_codigo(conexao)
        try:
            with persistencia.transacao(conexao):
                persistencia.inserir_reserva(
                    conexao, codigo=codigo, apartamento=apartamento, area=area, data=data
                )
                resultado = {"status": "ok", "reservado": True, "codigo": codigo, "area": area}
                ok = persistencia.transicionar_confirmacao(
                    conexao,
                    id=conf["id"],
                    session_id=conf["session_id"],
                    origem="aprovada",
                    destino="executada",
                    resultado=resultado,
                )
                if not ok:
                    raise RuntimeError(
                        f"confirmação {conf['id']!r} não estava mais 'aprovada' ao gravar "
                        "a reserva; execução concorrente inesperada"
                    )
        except sqlite3.IntegrityError:
            resultado = {"status": "ok", "reservado": False, "motivo": "data_ocupada"}
            with persistencia.transacao(conexao):
                persistencia.transicionar_confirmacao(
                    conexao,
                    id=conf["id"],
                    session_id=conf["session_id"],
                    origem="aprovada",
                    destino="falhou",
                    motivo="data_ocupada",
                    resultado=resultado,
                )
            return resultado
        return resultado

    def cancelar_reserva(codigo: str, tool_context: ToolContext) -> dict:
        """Cancela uma reserva do apartamento da sessão, sem confirmação.

        Só cancela se a reserva pertencer ao apartamento da sessão; se for
        de outro apartamento, não altera nada.

        Args:
            codigo: código da reserva a cancelar.
        """
        apartamento = tool_context.state["apartamento"]
        with persistencia.transacao(conexao):
            cancelado = persistencia.cancelar_reserva(conexao, codigo=codigo, apartamento=apartamento)
        return {"status": "ok", "cancelado": cancelado}

    return [
        FunctionTool(verificar_disponibilidade),
        FunctionTool(reservar_area, require_confirmation=require_confirmation_reservar_area),
        FunctionTool(cancelar_reserva),
    ]


def criar_tools_visitantes(conexao: sqlite3.Connection) -> list[FunctionTool]:
    """Fábrica das tools do especialista de visitantes."""

    def autorizar_visitante(nome: str, data: str, tool_context: ToolContext) -> dict:
        """Autoriza a entrada de um visitante no apartamento da sessão, numa data.

        Toda autorização de visitante libera acesso e por isso exige
        confirmação do morador pela rota de confirmações — mesmo que o
        morador diga que já confirmou no próprio texto da mensagem.

        Args:
            nome: nome do visitante.
            data: data da visita, no formato AAAA-MM-DD.
        """
        apartamento = tool_context.state["apartamento"]
        original_fc_id = tool_context.function_call_id
        conf = persistencia.obter_confirmacao_por_function_call_id(
            conexao, original_function_call_id=original_fc_id
        )
        if conf is None:
            raise RuntimeError(
                f"nenhuma confirmação encontrada para function_call_id={original_fc_id!r}; "
                "a pendência deveria ter sido criada por confirmacao.sincronizar_pendencias "
                "antes da aprovação"
            )
        if conf["estado"] == "executada":
            return conf["resultado"]  # reexecução "at least once": não grava de novo.
        if conf["estado"] != "aprovada":
            raise RuntimeError(
                f"autorizar_visitante reexecutada com confirmação em estado inesperado: "
                f"{conf['estado']!r} (esperava 'aprovada' ou 'executada')"
            )

        resultado = {"status": "ok", "autorizado": True}
        with persistencia.transacao(conexao):
            persistencia.autorizar_visitante(conexao, apartamento=apartamento, nome=nome, data=data)
            ok = persistencia.transicionar_confirmacao(
                conexao,
                id=conf["id"],
                session_id=conf["session_id"],
                origem="aprovada",
                destino="executada",
                resultado=resultado,
            )
            if not ok:
                raise RuntimeError(
                    f"confirmação {conf['id']!r} não estava mais 'aprovada' ao gravar "
                    "a autorização; execução concorrente inesperada"
                )
        return resultado

    return [FunctionTool(autorizar_visitante, require_confirmation=True)]


def criar_tools_regulamento(caminho_regulamento: str) -> list[FunctionTool]:
    """Fábrica da tool do especialista de regulamento. Carrega o índice
    uma vez, por fechamento, e nunca expõe o apartamento nem o texto
    completo do regulamento."""

    regulamento = Regulamento(caminho_regulamento)

    def consultar_regulamento(
        numero_artigo: str | None = None,
        termos_busca: str | None = None,
    ) -> dict:
        """Consulta o regulamento interno do condomínio, artigo por artigo.

        Devolve no máximo dois artigos por chamada, cada um com capítulo,
        número e texto — nunca o regulamento inteiro nem um capítulo
        inteiro. Use exatamente um dos dois parâmetros: `numero_artigo`
        quando já souber o número do artigo, ou `termos_busca` com
        palavras-chave (por exemplo "piscina domingo") que devem
        aparecer juntas no artigo, quando não souber o número.

        Args:
            numero_artigo: número do artigo do regulamento, se já conhecido.
            termos_busca: palavras-chave separadas por espaço para buscar
                no texto dos artigos.
        """
        if numero_artigo:
            artigo = regulamento.por_numero(numero_artigo)
            artigos = [artigo] if artigo is not None else []
        elif termos_busca:
            artigos = regulamento.buscar(termos_busca.split(), limite=2)
        else:
            return {
                "status": "erro",
                "mensagem": "informe numero_artigo ou termos_busca",
            }
        return {
            "status": "ok",
            "artigos": [
                {"capitulo": a.capitulo, "numero": a.numero, "texto": a.texto}
                for a in artigos
            ],
        }

    return [FunctionTool(consultar_regulamento)]
