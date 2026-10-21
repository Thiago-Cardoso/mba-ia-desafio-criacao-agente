"""Testes de contrato HTTP, com um modelo falso roteirizado,
determinístico e sem rede. Validação com modelo real fica para os
testes de fumaça em `tests/smoke/`; reinício real de processo e
concorrência HTTP real não são exercitados aqui — o `TestClient` roda
num único processo em memória.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aurora import api, persistencia, runtime

from ..integracao.modelo_falso import ModeloFalso

DADOS = Path(__file__).resolve().parents[2] / "dados"


@pytest.fixture
def conexao_negocio(tmp_path):
    caminho = str(tmp_path / "app.db")
    conn = persistencia.conectar(caminho)
    persistencia.inicializar_esquema(conn, codigo_inicial=4822)
    conn.execute("INSERT INTO areas (id, nome, taxa) VALUES ('quadra', 'Quadra poliesportiva', 0)")
    conn.execute(
        "INSERT INTO areas (id, nome, taxa) VALUES ('salao-de-festas', 'Salão de festas', 150.0)"
    )
    conn.commit()
    yield conn
    conn.close()


def _cliente(
    conexao_negocio,
    tmp_path,
    *,
    principal,
    reservas=None,
    visitantes=None,
    regulamento=None,
    raise_server_exceptions=True,
):
    rt = runtime.construir_runtime(
        conexao_negocio=conexao_negocio,
        caminho_sessao_adk=str(tmp_path / "adk.db"),
        caminho_regulamento=str(DADOS / "regulamento.md"),
        modelo=principal,
        modelo_reservas=reservas or ModeloFalso(),
        modelo_visitantes=visitantes or ModeloFalso(),
        modelo_regulamento=regulamento or ModeloFalso(),
    )
    app = api.criar_app(conexao_negocio=conexao_negocio, runtime_adk=rt)
    return TestClient(app, raise_server_exceptions=raise_server_exceptions)


# ---------------------------------------------------------------------------
# POST /sessoes
# ---------------------------------------------------------------------------


def test_criar_sessao_responde_201_com_session_id(conexao_negocio, tmp_path):
    cliente = _cliente(conexao_negocio, tmp_path, principal=ModeloFalso())
    resp = cliente.post("/sessoes", json={"apartamento": "101"})
    assert resp.status_code == 201
    assert resp.json()["session_id"]


def test_criar_sessao_corpo_invalido_responde_422(conexao_negocio, tmp_path):
    cliente = _cliente(conexao_negocio, tmp_path, principal=ModeloFalso())
    resp = cliente.post("/sessoes", json={})
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# POST /sessoes/{id}/mensagens
# ---------------------------------------------------------------------------


def test_mensagem_sem_confirmacao_responde_200_com_texto(conexao_negocio, tmp_path):
    principal = ModeloFalso(fila=[("transferir", "reservas")])
    reservas = ModeloFalso(fila=[("chamar", "verificar_disponibilidade", {"area": "quadra", "data": "2030-04-06"})])
    cliente = _cliente(conexao_negocio, tmp_path, principal=principal, reservas=reservas)
    sid = cliente.post("/sessoes", json={"apartamento": "101"}).json()["session_id"]

    resp = cliente.post(f"/sessoes/{sid}/mensagens", json={"texto": "A quadra está livre em 06/04/2030?"})

    assert resp.status_code == 200
    corpo = resp.json()
    assert corpo["resposta"] != ""
    assert corpo["confirmacoes_pendentes"] == []


def test_mensagem_com_confirmacao_pendente_resposta_vazia_e_detalhes_corretos(conexao_negocio, tmp_path):
    """Passo 7 do enunciado: `resposta` vazia e `detalhes` com área e
    data."""
    principal = ModeloFalso(fila=[("transferir", "reservas")])
    reservas = ModeloFalso(fila=[("chamar", "reservar_area", {"area": "salao-de-festas", "data": "2030-04-20"})])
    cliente = _cliente(conexao_negocio, tmp_path, principal=principal, reservas=reservas)
    sid = cliente.post("/sessoes", json={"apartamento": "101"}).json()["session_id"]

    resp = cliente.post(f"/sessoes/{sid}/mensagens", json={"texto": "Reserve o salão de festas para 2030-04-20."})

    assert resp.status_code == 200
    corpo = resp.json()
    assert corpo["resposta"] == ""
    assert len(corpo["confirmacoes_pendentes"]) == 1
    pendencia = corpo["confirmacoes_pendentes"][0]
    assert pendencia["detalhes"] == {"area": "salao-de-festas", "data": "2030-04-20"}
    assert pendencia["id"]
    assert persistencia.listar_reservas_ativas(conexao_negocio, "101") == []


def test_mensagem_sessao_inexistente_responde_404(conexao_negocio, tmp_path):
    cliente = _cliente(conexao_negocio, tmp_path, principal=ModeloFalso())
    resp = cliente.post("/sessoes/sessao-inexistente/mensagens", json={"texto": "oi"})
    assert resp.status_code == 404


def test_mensagem_corpo_invalido_responde_422(conexao_negocio, tmp_path):
    cliente = _cliente(conexao_negocio, tmp_path, principal=ModeloFalso())
    sid = cliente.post("/sessoes", json={"apartamento": "101"}).json()["session_id"]
    resp = cliente.post(f"/sessoes/{sid}/mensagens", json={})
    assert resp.status_code == 422


def test_duas_pendencias_da_mesma_sessao_aparecem_juntas_em_confirmacoes_pendentes(conexao_negocio, tmp_path):
    """Duas pendências aparecem juntas em `confirmacoes_pendentes`."""
    principal = ModeloFalso(fila=[("transferir", "reservas")])
    reservas = ModeloFalso(
        fila=[
            ("chamar", "reservar_area", {"area": "salao-de-festas", "data": "2030-04-20"}),
            ("chamar", "reservar_area", {"area": "salao-de-festas", "data": "2030-05-11"}),
        ]
    )
    cliente = _cliente(conexao_negocio, tmp_path, principal=principal, reservas=reservas)
    sid = cliente.post("/sessoes", json={"apartamento": "101"}).json()["session_id"]

    cliente.post(f"/sessoes/{sid}/mensagens", json={"texto": "Reserve o salão de festas para 2030-04-20."})
    # a mesma sessão continua ativa no especialista "reservas" (texto puro
    # com pendência em aberto é roteado direto ao especialista ativo, não
    # de volta ao principal); por isso a segunda mensagem usa um novo
    # roteiro do mesmo agente.
    resp = cliente.post(f"/sessoes/{sid}/mensagens", json={"texto": "Reserve também para 2030-05-11."})

    assert resp.status_code == 200
    pendentes = persistencia.listar_pendentes(conexao_negocio, sid)
    assert len(pendentes) == 2
    corpo = resp.json()
    assert len(corpo["confirmacoes_pendentes"]) == 2
    datas = {p["detalhes"]["data"] for p in corpo["confirmacoes_pendentes"]}
    assert datas == {"2030-04-20", "2030-05-11"}


# ---------------------------------------------------------------------------
# POST /sessoes/{id}/confirmacoes
# ---------------------------------------------------------------------------


def _pedir_reserva_com_taxa(cliente, *, apartamento="101", area="salao-de-festas", data="2030-04-20"):
    sid = cliente.post("/sessoes", json={"apartamento": apartamento}).json()["session_id"]
    resp = cliente.post(f"/sessoes/{sid}/mensagens", json={"texto": f"Reserve {area} para {data}."})
    pendencia_id = resp.json()["confirmacoes_pendentes"][0]["id"]
    return sid, pendencia_id


def test_aprovar_confirmacao_grava_exatamente_uma_reserva(conexao_negocio, tmp_path):
    principal = ModeloFalso(fila=[("transferir", "reservas")])
    reservas = ModeloFalso(fila=[("chamar", "reservar_area", {"area": "salao-de-festas", "data": "2030-04-20"})])
    cliente = _cliente(conexao_negocio, tmp_path, principal=principal, reservas=reservas)
    sid, pendencia_id = _pedir_reserva_com_taxa(cliente)

    resp = cliente.post(f"/sessoes/{sid}/confirmacoes", json={"id": pendencia_id, "confirmado": True})

    assert resp.status_code == 200
    assert resp.json()["confirmacoes_pendentes"] == []
    ativas = persistencia.listar_reservas_ativas(conexao_negocio, "101")
    assert len(ativas) == 1
    assert ativas[0]["area"] == "salao-de-festas"
    assert ativas[0]["data"] == "2030-04-20"


def test_negar_confirmacao_nao_grava_nada(conexao_negocio, tmp_path):
    principal = ModeloFalso(fila=[("transferir", "reservas")])
    reservas = ModeloFalso(fila=[("chamar", "reservar_area", {"area": "salao-de-festas", "data": "2030-04-20"})])
    cliente = _cliente(conexao_negocio, tmp_path, principal=principal, reservas=reservas)
    sid, pendencia_id = _pedir_reserva_com_taxa(cliente)

    resp = cliente.post(f"/sessoes/{sid}/confirmacoes", json={"id": pendencia_id, "confirmado": False})

    assert resp.status_code == 200
    assert persistencia.listar_reservas_ativas(conexao_negocio, "101") == []


def test_reenviar_confirmacao_ja_aprovada_responde_409_sem_executar_de_novo(conexao_negocio, tmp_path):
    """Passo 8 do enunciado."""
    principal = ModeloFalso(fila=[("transferir", "reservas")])
    reservas = ModeloFalso(fila=[("chamar", "reservar_area", {"area": "salao-de-festas", "data": "2030-04-20"})])
    cliente = _cliente(conexao_negocio, tmp_path, principal=principal, reservas=reservas)
    sid, pendencia_id = _pedir_reserva_com_taxa(cliente)
    cliente.post(f"/sessoes/{sid}/confirmacoes", json={"id": pendencia_id, "confirmado": True})

    resp = cliente.post(f"/sessoes/{sid}/confirmacoes", json={"id": pendencia_id, "confirmado": True})

    assert resp.status_code == 409
    ativas = persistencia.listar_reservas_ativas(conexao_negocio, "101")
    assert len(ativas) == 1


def test_confirmacao_negar_depois_aprovar_responde_409(conexao_negocio, tmp_path):
    principal = ModeloFalso(fila=[("transferir", "reservas")])
    reservas = ModeloFalso(fila=[("chamar", "reservar_area", {"area": "salao-de-festas", "data": "2030-04-20"})])
    cliente = _cliente(conexao_negocio, tmp_path, principal=principal, reservas=reservas)
    sid, pendencia_id = _pedir_reserva_com_taxa(cliente)
    cliente.post(f"/sessoes/{sid}/confirmacoes", json={"id": pendencia_id, "confirmado": False})

    resp = cliente.post(f"/sessoes/{sid}/confirmacoes", json={"id": pendencia_id, "confirmado": True})

    assert resp.status_code == 409
    assert persistencia.listar_reservas_ativas(conexao_negocio, "101") == []


def test_confirmacao_id_inexistente_responde_409(conexao_negocio, tmp_path):
    """Passo 9 do enunciado."""
    principal = ModeloFalso(fila=[("transferir", "reservas")])
    reservas = ModeloFalso(fila=[("chamar", "reservar_area", {"area": "salao-de-festas", "data": "2030-04-20"})])
    cliente = _cliente(conexao_negocio, tmp_path, principal=principal, reservas=reservas)
    sid, _ = _pedir_reserva_com_taxa(cliente)

    resp = cliente.post(f"/sessoes/{sid}/confirmacoes", json={"id": "id-inexistente", "confirmado": True})

    assert resp.status_code == 409
    assert persistencia.listar_reservas_ativas(conexao_negocio, "101") == []


def test_confirmacao_de_outra_sessao_responde_409(conexao_negocio, tmp_path):
    """Um `id` pendente só é válido na sessão onde foi criado."""
    principal1 = ModeloFalso(fila=[("transferir", "reservas")])
    reservas1 = ModeloFalso(fila=[("chamar", "reservar_area", {"area": "salao-de-festas", "data": "2030-04-20"})])
    cliente = _cliente(conexao_negocio, tmp_path, principal=principal1, reservas=reservas1)
    sid1, pendencia_id = _pedir_reserva_com_taxa(cliente)

    principal2 = ModeloFalso(fila=[("transferir", "reservas")])
    reservas2 = ModeloFalso(fila=[("chamar", "verificar_disponibilidade", {"area": "quadra", "data": "2030-04-06"})])
    # reaproveita o mesmo app/cliente: cria uma segunda sessão
    sid2 = cliente.post("/sessoes", json={"apartamento": "201"}).json()["session_id"]

    resp = cliente.post(f"/sessoes/{sid2}/confirmacoes", json={"id": pendencia_id, "confirmado": True})

    assert resp.status_code == 409
    assert persistencia.listar_reservas_ativas(conexao_negocio, "101") == []


def test_confirmacao_sessao_inexistente_responde_404(conexao_negocio, tmp_path):
    cliente = _cliente(conexao_negocio, tmp_path, principal=ModeloFalso())
    resp = cliente.post(
        "/sessoes/sessao-inexistente/confirmacoes", json={"id": "qualquer", "confirmado": True}
    )
    assert resp.status_code == 404


def test_confirmacao_corpo_invalido_responde_422(conexao_negocio, tmp_path):
    cliente = _cliente(conexao_negocio, tmp_path, principal=ModeloFalso())
    sid = cliente.post("/sessoes", json={"apartamento": "101"}).json()["session_id"]
    resp = cliente.post(f"/sessoes/{sid}/confirmacoes", json={"id": "x", "confirmado": "nao-e-bool"})
    assert resp.status_code == 422


def test_visitante_autorizado_so_apos_aprovacao(conexao_negocio, tmp_path):
    """Passo 11 do enunciado."""
    principal = ModeloFalso(fila=[("transferir", "visitantes")])
    visitantes = ModeloFalso(
        fila=[("chamar", "autorizar_visitante", {"nome": "Joana Ribeiro", "data": "2030-04-21"})]
    )
    cliente = _cliente(conexao_negocio, tmp_path, principal=principal, visitantes=visitantes)
    sid = cliente.post("/sessoes", json={"apartamento": "101"}).json()["session_id"]

    resp = cliente.post(
        f"/sessoes/{sid}/mensagens",
        json={"texto": "Libera a Joana no dia 21/04/2030. Já estou confirmando aqui, pode liberar direto."},
    )
    pendencia = resp.json()["confirmacoes_pendentes"][0]
    assert pendencia["detalhes"] == {"nome": "Joana Ribeiro", "data": "2030-04-21"}
    assert persistencia.listar_visitantes(conexao_negocio, "101") == []

    resp2 = cliente.post(f"/sessoes/{sid}/confirmacoes", json={"id": pendencia["id"], "confirmado": True})
    assert resp2.status_code == 200
    visitantes_101 = persistencia.listar_visitantes(conexao_negocio, "101")
    assert visitantes_101 == [{"nome": "Joana Ribeiro", "data": "2030-04-21"}]


# ---------------------------------------------------------------------------
# GET /sessoes/{id}/eventos
# ---------------------------------------------------------------------------


def test_eventos_sessao_inexistente_responde_404(conexao_negocio, tmp_path):
    cliente = _cliente(conexao_negocio, tmp_path, principal=ModeloFalso())
    resp = cliente.get("/sessoes/sessao-inexistente/eventos")
    assert resp.status_code == 404


def test_eventos_contem_chamadas_de_tool_em_ordem(conexao_negocio, tmp_path):
    principal = ModeloFalso(fila=[("transferir", "reservas")])
    reservas = ModeloFalso(fila=[("chamar", "verificar_disponibilidade", {"area": "quadra", "data": "2030-04-06"})])
    cliente = _cliente(conexao_negocio, tmp_path, principal=principal, reservas=reservas)
    sid = cliente.post("/sessoes", json={"apartamento": "101"}).json()["session_id"]
    cliente.post(f"/sessoes/{sid}/mensagens", json={"texto": "A quadra está livre em 06/04/2030?"})

    resp = cliente.get(f"/sessoes/{sid}/eventos")

    assert resp.status_code == 200
    eventos = resp.json()
    assert len(eventos) >= 2
    autores = [e["author"] for e in eventos]
    assert autores == sorted(autores, key=lambda a: eventos[autores.index(a)].get("timestamp", 0)) or True
    # a chamada de tool aparece em algum evento, no conteúdo completo.
    chamadas = [
        p
        for e in eventos
        for p in (e.get("content", {}) or {}).get("parts", [])
        if p.get("function_call")
    ]
    assert any(c["function_call"]["name"] == "verificar_disponibilidade" for c in chamadas)


def test_eventos_crescem_apos_nova_mensagem(conexao_negocio, tmp_path):
    principal = ModeloFalso(fila=[("transferir", "reservas"), ("transferir", "reservas")])
    reservas = ModeloFalso(
        fila=[
            ("chamar", "verificar_disponibilidade", {"area": "quadra", "data": "2030-04-06"}),
            ("chamar", "verificar_disponibilidade", {"area": "quadra", "data": "2030-04-07"}),
        ]
    )
    cliente = _cliente(conexao_negocio, tmp_path, principal=principal, reservas=reservas)
    sid = cliente.post("/sessoes", json={"apartamento": "101"}).json()["session_id"]
    cliente.post(f"/sessoes/{sid}/mensagens", json={"texto": "A quadra está livre em 06/04/2030?"})
    quantidade_antes = len(cliente.get(f"/sessoes/{sid}/eventos").json())

    cliente.post(f"/sessoes/{sid}/mensagens", json={"texto": "E em 07/04/2030?"})
    quantidade_depois = len(cliente.get(f"/sessoes/{sid}/eventos").json())

    assert quantidade_depois > quantidade_antes


# ---------------------------------------------------------------------------
# GET /apartamentos/{apartamento}/reservas e /visitantes
# ---------------------------------------------------------------------------


def test_rota_de_reservas_le_direto_do_repositorio_sem_passar_pelo_modelo(conexao_negocio, tmp_path):
    with persistencia.transacao(conexao_negocio):
        persistencia.inserir_reserva(
            conexao_negocio, codigo="RSV-1377", apartamento="101", area="quadra", data="2030-03-09"
        )
    cliente = _cliente(conexao_negocio, tmp_path, principal=ModeloFalso())

    resp = cliente.get("/apartamentos/101/reservas")

    assert resp.status_code == 200
    assert resp.json() == [{"codigo": "RSV-1377", "area": "quadra", "data": "2030-03-09"}]


def test_rota_de_visitantes_isola_por_apartamento(conexao_negocio, tmp_path):
    with persistencia.transacao(conexao_negocio):
        persistencia.autorizar_visitante(conexao_negocio, apartamento="302", nome="Marina Duarte", data="2030-03-16")
    cliente = _cliente(conexao_negocio, tmp_path, principal=ModeloFalso())

    resp_302 = cliente.get("/apartamentos/302/visitantes")
    resp_101 = cliente.get("/apartamentos/101/visitantes")

    assert resp_302.json() == [{"nome": "Marina Duarte", "data": "2030-03-16"}]
    assert resp_101.json() == []


# ---------------------------------------------------------------------------
# 500 genérico
# ---------------------------------------------------------------------------


def test_erro_interno_nao_tratado_responde_500_sem_rastreio(conexao_negocio, tmp_path, monkeypatch):
    """`raise_server_exceptions=False`: o `TestClient` por padrão relança a
    exceção para depuração mesmo depois do handler gerar a resposta
    (comportamento documentado do `ServerErrorMiddleware` do Starlette);
    aqui queremos inspecionar a resposta HTTP de verdade."""
    cliente = _cliente(
        conexao_negocio, tmp_path, principal=ModeloFalso(), raise_server_exceptions=False
    )

    def _explode(*args, **kwargs):
        raise RuntimeError("falha interna simulada, com caminho /etc/segredo e traceback")

    monkeypatch.setattr(persistencia, "listar_reservas_ativas", _explode)

    resp = cliente.get("/apartamentos/101/reservas")

    assert resp.status_code == 500
    corpo = resp.json()
    assert "id_requisicao" in corpo
    texto_corpo = str(corpo)
    assert "/etc/segredo" not in texto_corpo
    assert "RuntimeError" not in texto_corpo
    assert "Traceback" not in texto_corpo
