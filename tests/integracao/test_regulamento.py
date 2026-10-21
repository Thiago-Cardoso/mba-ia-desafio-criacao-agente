"""Testes de integração do especialista de regulamento, com um modelo
falso roteirizado, determinístico e sem rede. Resposta do modelo real
fica para os testes de fumaça em `tests/smoke/`.
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

from google.genai import types

from aurora import agentes, persistencia, runtime, tools
from aurora.regulamento import Regulamento

from .modelo_falso import ModeloFalso

DADOS = Path(__file__).resolve().parents[2] / "dados"
CAMINHO_REGULAMENTO = DADOS / "regulamento.md"


def _tool(lista, nome):
    return next(t for t in lista if t.name == nome)


def _hash(caminho: Path) -> str:
    return hashlib.sha256(caminho.read_bytes()).hexdigest()


async def _drain(runner, session_id, texto):
    eventos = []
    msg = types.Content(role="user", parts=[types.Part(text=texto)])
    async for ev in runner.run_async(user_id="default", session_id=session_id, new_message=msg):
        eventos.append(ev)
    return eventos


def _chamadas(eventos, nome=None):
    cs = [fc for e in eventos for fc in e.get_function_calls()]
    return [c for c in cs if nome is None or c.name == nome]


def _respostas(eventos, nome=None):
    rs = [fr for e in eventos for fr in e.get_function_responses()]
    return [r for r in rs if nome is None or r.name == nome]


# ---------------------------------------------------------------------------
# Carregador: 96 artigos, 14 capítulos, somente leitura (unitário)
# ---------------------------------------------------------------------------


def test_carregador_conta_96_artigos_e_14_capitulos():
    regulamento = Regulamento(CAMINHO_REGULAMENTO)
    assert regulamento.quantidade_artigos() == 96
    assert regulamento.quantidade_capitulos() == 14


def test_carregador_nao_altera_o_arquivo_no_disco():
    """O hash de `dados/regulamento.md` não muda depois da leitura do
    carregador."""
    antes = _hash(CAMINHO_REGULAMENTO)
    Regulamento(CAMINHO_REGULAMENTO)
    depois = _hash(CAMINHO_REGULAMENTO)
    assert antes == depois


def test_por_numero_devolve_artigo_22_sobre_horario_da_piscina():
    regulamento = Regulamento(CAMINHO_REGULAMENTO)
    artigo = regulamento.por_numero("22")
    assert artigo is not None
    assert artigo.capitulo.startswith("Capítulo IV")
    assert "domingos e feriados" in artigo.texto.lower()
    assert "9h às 20h" in artigo.texto


def test_buscar_por_termos_limita_a_dois_artigos_mesmo_com_mais_combinacoes():
    regulamento = Regulamento(CAMINHO_REGULAMENTO)
    # "piscina" aparece em todo o capítulo IV (Art. 21 a 28): mais de dois
    # artigos combinam, mas o limite nunca é ultrapassado.
    encontrados = regulamento.buscar(["piscina"], limite=2)
    assert len(encontrados) == 2


def test_buscar_por_termos_especificos_encontra_o_artigo_da_piscina_aos_domingos():
    regulamento = Regulamento(CAMINHO_REGULAMENTO)
    encontrados = regulamento.buscar(["piscina", "domingo"], limite=2)
    numeros = {a.numero for a in encontrados}
    assert "22" in numeros
    for artigo in encontrados:
        assert artigo.capitulo.startswith("Capítulo IV")


def test_por_numero_inexistente_devolve_none():
    regulamento = Regulamento(CAMINHO_REGULAMENTO)
    assert regulamento.por_numero("999") is None


# ---------------------------------------------------------------------------
# Tool `consultar_regulamento`: contrato e limites (nível de tool)
# ---------------------------------------------------------------------------


def test_tool_consultar_regulamento_por_numero():
    tool = _tool(tools.criar_tools_regulamento(str(CAMINHO_REGULAMENTO)), "consultar_regulamento")
    resultado = tool.func(numero_artigo="22")
    assert resultado["status"] == "ok"
    assert len(resultado["artigos"]) == 1
    assert resultado["artigos"][0]["numero"] == "22"
    assert "9h às 20h" in resultado["artigos"][0]["texto"]


def test_tool_consultar_regulamento_por_termos_respeita_limite_de_dois():
    tool = _tool(tools.criar_tools_regulamento(str(CAMINHO_REGULAMENTO)), "consultar_regulamento")
    resultado = tool.func(termos_busca="piscina")
    assert resultado["status"] == "ok"
    assert len(resultado["artigos"]) <= 2


def test_tool_consultar_regulamento_sem_parametros_retorna_erro_estruturado():
    tool = _tool(tools.criar_tools_regulamento(str(CAMINHO_REGULAMENTO)), "consultar_regulamento")
    resultado = tool.func()
    assert resultado["status"] == "erro"


def test_tool_consultar_regulamento_nao_tem_parametro_de_apartamento():
    tool = _tool(tools.criar_tools_regulamento(str(CAMINHO_REGULAMENTO)), "consultar_regulamento")
    decl = tool._get_declaration()
    esquema = decl.model_dump(exclude_none=True).get("parameters_json_schema") or {}
    propriedades = esquema.get("properties", {})
    assert "apartamento" not in propriedades


# ---------------------------------------------------------------------------
# Topologia completa: transferência ao especialista de regulamento
# ---------------------------------------------------------------------------


def _construir_runtime(tmp_path, *, principal, regulamento):
    conn = persistencia.conectar(str(tmp_path / "app.db"))
    persistencia.inicializar_esquema(conn, codigo_inicial=4822)
    conn.commit()
    return conn, runtime.construir_runtime(
        conexao_negocio=conn,
        caminho_sessao_adk=str(tmp_path / "adk.db"),
        caminho_regulamento=str(CAMINHO_REGULAMENTO),
        modelo=principal,
        modelo_regulamento=regulamento,
    )


def test_pergunta_sobre_piscina_aos_domingos_traz_o_horario_do_art_22(tmp_path):
    principal = ModeloFalso(fila=[("transferir", "regulamento")])
    regulamento = ModeloFalso(
        fila=[("chamar", "consultar_regulamento", {"termos_busca": "piscina domingo"})]
    )
    conn, rt = _construir_runtime(tmp_path, principal=principal, regulamento=regulamento)
    sid = asyncio.run(runtime.criar_sessao(rt, apartamento="101"))

    eventos = asyncio.run(_drain(rt.runner, sid, "A piscina abre que horas aos domingos?"))

    assert "regulamento" in [e.author for e in eventos]
    respostas = _respostas(eventos, "consultar_regulamento")
    assert len(respostas) == 1
    artigos = respostas[0].response["artigos"]
    numeros = {a["numero"] for a in artigos}
    assert "22" in numeros
    texto_art_22 = next(a["texto"] for a in artigos if a["numero"] == "22")
    assert "9h às 20h" in texto_art_22
    conn.close()


def test_eventos_da_consulta_tem_no_maximo_dois_artigos_e_so_do_capitulo_pedido(tmp_path):
    """Os eventos persistidos não contêm trechos de capítulos que tratam
    de outros assuntos — só os artigos efetivamente devolvidos."""
    principal = ModeloFalso(fila=[("transferir", "regulamento")])
    regulamento = ModeloFalso(fila=[("chamar", "consultar_regulamento", {"termos_busca": "piscina"})])
    conn, rt = _construir_runtime(tmp_path, principal=principal, regulamento=regulamento)
    sid = asyncio.run(runtime.criar_sessao(rt, apartamento="101"))

    eventos = asyncio.run(_drain(rt.runner, sid, "Quais as regras da piscina?"))

    respostas = _respostas(eventos, "consultar_regulamento")
    assert len(respostas) == 1
    artigos = respostas[0].response["artigos"]
    assert len(artigos) <= 2
    capitulos = {a["capitulo"] for a in artigos}
    assert capitulos <= {"Capítulo IV: Piscina"}
    # nenhum outro capítulo (por exemplo o de garagem ou animais) aparece
    # na resposta da tool nem, portanto, nos eventos da sessão.
    texto_eventos = str([a["texto"] for a in artigos])
    assert "Capítulo XI" not in texto_eventos
    assert "Capítulo VIII" not in texto_eventos
    conn.close()


def test_regulamento_nao_grava_nada_no_banco_de_negocio(tmp_path):
    """A consulta ao regulamento é somente leitura: não cria reserva,
    visitante ou confirmação (não há `require_confirmation` nesta tool)."""
    principal = ModeloFalso(fila=[("transferir", "regulamento")])
    regulamento = ModeloFalso(fila=[("chamar", "consultar_regulamento", {"numero_artigo": "22"})])
    conn, rt = _construir_runtime(tmp_path, principal=principal, regulamento=regulamento)
    sid = asyncio.run(runtime.criar_sessao(rt, apartamento="101"))

    eventos = asyncio.run(_drain(rt.runner, sid, "Horário da piscina aos domingos?"))

    assert _chamadas(eventos, "adk_request_confirmation") == []
    assert persistencia.listar_reservas_ativas(conn, "101") == []
    assert persistencia.listar_visitantes(conn, "101") == []
    conn.close()
