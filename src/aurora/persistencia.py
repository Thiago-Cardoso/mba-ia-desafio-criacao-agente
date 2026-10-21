"""Persistência de negócio: SQLite para reservas, visitantes, confirmações e
o contador de códigos de reserva.

Não depende do ADK. Implementa o esquema, o índice único parcial e as
transições atômicas de confirmação: a execução única por confirmação é
garantida pela aplicação, nunca pelo ADK.

Convenção de transação: as funções de escrita (`inserir_reserva`,
`cancelar_reserva`, `autorizar_visitante`, `criar_confirmacao_pendente`,
`transicionar_confirmacao`) **não** abrem nem fecham transação. Elas devem
ser chamadas dentro do gerenciador de contexto `transacao()`, para que o
chamador (por exemplo, compondo a gravação de uma reserva com a transição
`aprovada -> executada` na mesma transação) controle o limite exato da
unidade atômica. A única exceção é `proximo_codigo`, que é atômica em si
mesma (ver docstring).
"""

from __future__ import annotations

import datetime as _dt
import json
import sqlite3
import unicodedata
from contextlib import contextmanager
from typing import Any, Iterator, Optional

# Transições válidas do registro de confirmações.
TRANSICOES_VALIDAS = {
    ("pendente", "aprovada"),
    ("pendente", "negada"),
    ("aprovada", "executada"),
    ("aprovada", "falhou"),
    ("aprovada", "interrompida"),
}

ESTADOS_TERMINAIS = ("negada", "executada", "falhou", "interrompida")

_DDL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS apartamentos (
    numero TEXT PRIMARY KEY,
    morador TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS areas (
    id TEXT PRIMARY KEY,
    nome TEXT NOT NULL,
    taxa REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS reservas (
    codigo TEXT PRIMARY KEY,
    apartamento TEXT NOT NULL,
    area TEXT NOT NULL,
    data TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('ativa', 'cancelada')),
    criada_em TEXT NOT NULL,
    cancelada_em TEXT
);

-- No máximo uma reserva ATIVA por área e data. Reservas canceladas não
-- entram no índice (ver o comentário de cancelar_reserva sobre o que
-- isso implica para datas liberadas por cancelamento).
CREATE UNIQUE INDEX IF NOT EXISTS ux_reservas_ativa
    ON reservas(area, data) WHERE status = 'ativa';

CREATE TABLE IF NOT EXISTS visitantes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    apartamento TEXT NOT NULL,
    nome TEXT NOT NULL,
    data TEXT NOT NULL,
    criado_em TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS confirmacoes (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    apartamento TEXT NOT NULL,
    acao TEXT NOT NULL,
    area TEXT,
    data TEXT,
    nome TEXT,
    estado TEXT NOT NULL CHECK (estado IN
        ('pendente', 'aprovada', 'negada', 'executada', 'falhou', 'interrompida')),
    adk_confirmation_fc_id TEXT,
    original_function_call_id TEXT,
    motivo TEXT,
    -- Resultado final da tool, em JSON (ex.: {"reservado": true, "codigo": "RSV-4822"}).
    -- Gravado na mesma transação que marca 'executada' ou 'falhou'. Permite
    -- que uma reexecução da tool pelo ADK ("at least once") devolva o mesmo
    -- resultado sem gravar de novo (idempotência).
    resultado TEXT,
    criada_em TEXT NOT NULL,
    decidida_em TEXT,
    executada_em TEXT
);

CREATE INDEX IF NOT EXISTS ix_confirmacoes_sessao_estado
    ON confirmacoes(session_id, estado);
CREATE INDEX IF NOT EXISTS ix_confirmacoes_original_fc
    ON confirmacoes(original_function_call_id);

CREATE TABLE IF NOT EXISTS contador_codigo (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    proximo INTEGER NOT NULL
);
"""


def conectar(caminho: str) -> sqlite3.Connection:
    """Abre uma conexão com WAL, busy_timeout e controle manual de
    transação.

    `isolation_level=None` desliga o autocommit implícito do módulo
    `sqlite3`, para que `transacao()` controle `BEGIN IMMEDIATE` de forma
    explícita, em vez de depender do comportamento implícito do driver.

    `check_same_thread=False`: a conexão é criada uma vez e usada
    pelas rotas da API, que no FastAPI podem rodar numa thread de
    threadpool diferente da que criou a conexão. A exclusividade de
    escrita continua garantida pelo WAL e pelas transações curtas
    (`transacao()`), não pelo controle de thread do driver — o SQLite em
    modo WAL já serializa escritores de qualquer thread ou processo.
    """
    conn = sqlite3.connect(caminho, isolation_level=None, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def inicializar_esquema(conn: sqlite3.Connection, *, codigo_inicial: int) -> None:
    """Cria o esquema se não existir. Idempotente: pode ser chamada em toda
    subida do processo sem efeito além da primeira vez.

    `codigo_inicial` só é usado na primeira inicialização (quando
    `contador_codigo` ainda não tem registro). O chamador deve passar um
    valor maior que todo número de código já presente em
    `dados/reservas.json` — por exemplo, 1 + o maior número entre
    `RSV-1377`, `RSV-2950` e `RSV-4821`.
    """
    conn.executescript(_DDL)
    conn.execute(
        "INSERT INTO contador_codigo (id, proximo) VALUES (1, ?) "
        "ON CONFLICT(id) DO NOTHING",
        (codigo_inicial,),
    )
    conn.commit()


@contextmanager
def transacao(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Transação de escrita curta, com `BEGIN IMMEDIATE`, para serializar
    escritores concorrentes no mesmo arquivo.

    Comita ao sair sem exceção. Em qualquer exceção — inclusive
    `sqlite3.IntegrityError` por violação do índice único parcial de
    reservas ativas — desfaz e repropaga, para o chamador decidir o
    tratamento (`ROLLBACK` e transição para `falhou` com
    `motivo='data_ocupada'`, por exemplo).
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()


# ---------------------------------------------------------------------------
# Contador de códigos
# ---------------------------------------------------------------------------


def proximo_codigo(conn: sqlite3.Connection) -> str:
    """Gera o próximo código de reserva, de forma atômica e durável.

    O formato é livre; usamos ``RSV-<inteiro>``, crescente, nunca
    reiniciado pela restauração (que não toca nesta tabela) e nunca
    reutilizado — a chave primária de `reservas` garante
    isso mesmo que o contador seja corrompido, e o próprio contador nunca
    decresce.

    Esta função é atômica em si mesma (abre e fecha sua própria transação)
    porque reservar um código antes de saber se a gravação da reserva vai
    ter sucesso é seguro: se a gravação falhar depois (por exemplo, por
    `data_ocupada`), o código fica apenas sem uso — não há problema, porque
    a garantia exigida é "nunca repete", nunca "sem lacunas".
    """
    with transacao(conn):
        row = conn.execute("SELECT proximo FROM contador_codigo WHERE id = 1").fetchone()
        numero = row["proximo"]
        conn.execute(
            "UPDATE contador_codigo SET proximo = ? WHERE id = 1 AND proximo = ?",
            (numero + 1, numero),
        )
    return f"RSV-{numero}"


# ---------------------------------------------------------------------------
# Carga inicial e restauração
# ---------------------------------------------------------------------------


def carregar_apartamentos_e_areas(
    conn: sqlite3.Connection, *, apartamentos: list[dict], areas: list[dict]
) -> None:
    """Carrega apartamentos e áreas de `dados/` (tabelas de referência,
    nunca alteradas pelo uso da aplicação). Idempotente: `ON CONFLICT
    DO NOTHING`, para que `subir` possa chamar isto em toda subida sem
    duplicar nem sobrescrever uma linha já carregada. Abre sua própria
    transação (chamada isolada, fora do fluxo de uma tool)."""
    with transacao(conn):
        for apartamento in apartamentos:
            conn.execute(
                "INSERT INTO apartamentos (numero, morador) VALUES (?, ?) "
                "ON CONFLICT(numero) DO NOTHING",
                (apartamento["numero"], apartamento["morador"]),
            )
        for area in areas:
            conn.execute(
                "INSERT INTO areas (id, nome, taxa) VALUES (?, ?, ?) "
                "ON CONFLICT(id) DO NOTHING",
                (area["id"], area["nome"], area["taxa"]),
            )


def restaurar_reservas_e_visitantes(
    conn: sqlite3.Connection, *, reservas: list[dict], visitantes: list[dict]
) -> dict:
    """Restauração: apaga e recria `reservas` e `visitantes` a partir de
    `dados/`, em uma única transação. Nunca toca em `apartamentos`,
    `areas`, sessões, eventos do ADK, `confirmacoes` nem em
    `contador_codigo` — o chamador (`restaurar.py`) é responsável por
    recusar a operação antes de chamar esta função se houver uma
    confirmação `aprovada` sem execução.

    Devolve as contagens de remoção/recriação, para a saída do comando
    anunciar o que fez — nunca altera dados sem reportar o que mudou."""
    with transacao(conn):
        removidas = conn.execute("SELECT COUNT(*) AS n FROM reservas").fetchone()["n"]
        removidos = conn.execute("SELECT COUNT(*) AS n FROM visitantes").fetchone()["n"]
        conn.execute("DELETE FROM reservas")
        conn.execute("DELETE FROM visitantes")
        agora = _agora()
        for reserva in reservas:
            conn.execute(
                "INSERT INTO reservas (codigo, apartamento, area, data, status, criada_em) "
                "VALUES (?, ?, ?, ?, 'ativa', ?)",
                (reserva["codigo"], reserva["apartamento"], reserva["area"], reserva["data"], agora),
            )
        for visitante in visitantes:
            conn.execute(
                "INSERT INTO visitantes (apartamento, nome, data, criado_em) VALUES (?, ?, ?, ?)",
                (visitante["apartamento"], visitante["nome"], visitante["data"], agora),
            )
    return {
        "reservas_removidas": removidas,
        "reservas_recriadas": len(reservas),
        "visitantes_removidos": removidos,
        "visitantes_recriados": len(visitantes),
    }


# ---------------------------------------------------------------------------
# Reservas
# ---------------------------------------------------------------------------


def inserir_reserva(
    conn: sqlite3.Connection, *, codigo: str, apartamento: str, area: str, data: str
) -> None:
    """Grava uma reserva ativa dentro da transação aberta pelo chamador.

    Levanta `sqlite3.IntegrityError` se a área já tiver reserva ativa na
    mesma data (índice único parcial) ou se o código já existir (chave
    primária).
    """
    conn.execute(
        "INSERT INTO reservas (codigo, apartamento, area, data, status, criada_em) "
        "VALUES (?, ?, ?, ?, 'ativa', ?)",
        (codigo, apartamento, area, data, _agora()),
    )


def cancelar_reserva(conn: sqlite3.Connection, *, codigo: str, apartamento: str) -> bool:
    """Cancela uma reserva ativa do apartamento informado, dentro da
    transação aberta pelo chamador.

    Só cancela se `apartamento` casar e o status atual for `ativa`, em uma
    única instrução `UPDATE ... WHERE` — defesa de identidade em
    profundidade a nível de repositório (a origem confiável do apartamento
    vem da sessão, nunca do modelo).

    **Reuso de data após cancelamento.** Cancelar uma reserva a tira do
    índice único parcial (que só cobre `status = 'ativa'`), então a área
    e a data ficam livres para uma nova reserva — consequência direta do
    desenho do índice, não uma decisão de produto deliberada. Se uma data
    liberada por cancelamento deve mesmo poder ser reservada de novo é
    uma pergunta de produto ainda **em aberto**; esta função não a
    resolve, só não a impede. O teste que exercita esse reuso fica
    marcado como bloqueado (ver `tests/unit/persistencia/test_persistencia.py`).

    Retorna `True` se uma linha foi alterada, `False` caso contrário
    (reserva inexistente, já cancelada, ou de outro apartamento).
    """
    cur = conn.execute(
        "UPDATE reservas SET status = 'cancelada', cancelada_em = ? "
        "WHERE codigo = ? AND apartamento = ? AND status = 'ativa'",
        (_agora(), codigo, apartamento),
    )
    return cur.rowcount == 1


def listar_reservas_ativas(conn: sqlite3.Connection, apartamento: str) -> list[dict]:
    """Lista as reservas ativas do apartamento, no formato da rota de
    verificação do enunciado (`codigo`, `area`, `data`)."""
    rows = conn.execute(
        "SELECT codigo, area, data FROM reservas "
        "WHERE apartamento = ? AND status = 'ativa' ORDER BY codigo",
        (apartamento,),
    ).fetchall()
    return [dict(r) for r in rows]


def area_disponivel(conn: sqlite3.Connection, *, area: str, data: str) -> bool:
    """`True` se não houver reserva ativa para a área nessa data. Uso
    previsto: consulta de disponibilidade, que só pode devolver
    livre/ocupada, nunca de quem é a reserva."""
    row = conn.execute(
        "SELECT 1 FROM reservas WHERE area = ? AND data = ? AND status = 'ativa'",
        (area, data),
    ).fetchone()
    return row is None


def _normalizar_nome_area(texto: str) -> str:
    """Remove acentos, baixa a caixa e remove espaços nas bordas — usada
    só para comparar o nome legível de uma área, nunca o `id`. O modelo
    pode enviar o nome em português em vez do id canônico, então a
    comparação precisa tolerar essa variação sem ser literal."""
    sem_acento = unicodedata.normalize("NFKD", texto)
    sem_acento = "".join(c for c in sem_acento if not unicodedata.combining(c))
    return sem_acento.strip().casefold()


def resolver_area(conn: sqlite3.Connection, area_informada: str) -> tuple[Optional[dict], Optional[str]]:
    """Resolve o argumento `area` — gerado pelo modelo e portanto não
    confiável — contra a tabela oficial `areas`, carregada de
    `dados/areas.json` (`carregar_apartamentos_e_areas`).

    Aceita o `id` canônico com correspondência exata (prioridade); se não
    bater, tenta o nome legível (`nome`) com correspondência EXATA depois
    de normalizar acentos/caixa/espaços — nunca correspondência parcial
    ou por similaridade, para nunca inventar uma correspondência.

    Devolve `(linha, None)` quando há exatamente uma área correspondente
    (`linha` com `id`, `nome`, `taxa`), ou `(None, motivo)` quando a área
    é desconhecida (`motivo="area_desconhecida"`) ou ambígua
    (`motivo="area_ambigua"`, hoje impossível com os dados atuais, mas o
    contrato existe para não quebrar silenciosamente se isso mudar).
    """
    row = conn.execute("SELECT id, nome, taxa FROM areas WHERE id = ?", (area_informada,)).fetchone()
    if row is not None:
        return dict(row), None

    alvo = _normalizar_nome_area(area_informada)
    candidatos = [
        dict(r)
        for r in conn.execute("SELECT id, nome, taxa FROM areas").fetchall()
        if _normalizar_nome_area(r["nome"]) == alvo
    ]
    if len(candidatos) == 1:
        return candidatos[0], None
    if len(candidatos) > 1:
        return None, "area_ambigua"
    return None, "area_desconhecida"


def listar_areas(conn: sqlite3.Connection) -> list[dict]:
    """Lista as áreas oficiais (`id`, `nome`, `taxa`), para o especialista
    de reservas oferecer ao modelo sem que ele precise adivinhar o id."""
    rows = conn.execute("SELECT id, nome, taxa FROM areas ORDER BY id").fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Visitantes
# ---------------------------------------------------------------------------


def autorizar_visitante(
    conn: sqlite3.Connection, *, apartamento: str, nome: str, data: str
) -> int:
    """Registra uma autorização de visitante, dentro da transação aberta
    pelo chamador. Se duas autorizações com o mesmo nome e data devem
    ser tratadas como duplicata é uma decisão de produto ainda aberta:
    esta função não impõe unicidade — nenhuma regra é inventada aqui."""
    cur = conn.execute(
        "INSERT INTO visitantes (apartamento, nome, data, criado_em) VALUES (?, ?, ?, ?)",
        (apartamento, nome, data, _agora()),
    )
    return cur.lastrowid


def listar_visitantes(conn: sqlite3.Connection, apartamento: str) -> list[dict]:
    rows = conn.execute(
        "SELECT nome, data FROM visitantes WHERE apartamento = ? ORDER BY id",
        (apartamento,),
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Registro de confirmações
# ---------------------------------------------------------------------------


def criar_confirmacao_pendente(
    conn: sqlite3.Connection,
    *,
    id: str,
    session_id: str,
    apartamento: str,
    acao: str,
    area: Optional[str] = None,
    data: Optional[str] = None,
    nome: Optional[str] = None,
    original_function_call_id: Optional[str] = None,
    adk_confirmation_fc_id: Optional[str] = None,
) -> None:
    """Cria uma linha `pendente`, dentro da transação aberta pelo chamador.

    Levanta `sqlite3.IntegrityError` se o `id` já existir — a reconciliação
    idempotente (não recriar uma pendência já observada) é responsabilidade
    de quem observa os eventos do ADK, não desta função.
    """
    conn.execute(
        "INSERT INTO confirmacoes "
        "(id, session_id, apartamento, acao, area, data, nome, estado, "
        " adk_confirmation_fc_id, original_function_call_id, criada_em) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'pendente', ?, ?, ?)",
        (
            id,
            session_id,
            apartamento,
            acao,
            area,
            data,
            nome,
            adk_confirmation_fc_id,
            original_function_call_id,
            _agora(),
        ),
    )


def transicionar_confirmacao(
    conn: sqlite3.Connection,
    *,
    id: str,
    session_id: str,
    origem: str,
    destino: str,
    motivo: Optional[str] = None,
    resultado: Optional[dict[str, Any]] = None,
) -> bool:
    """Transição atômica de estado de uma confirmação, dentro da
    transação aberta pelo chamador.

    Só altera a linha se `id`, `session_id` e o estado atual (`origem`)
    casarem exatamente — por isso serve tanto para a aprovação/negação
    quanto para marcar `falhou`/`interrompida`. A execução de negócio em si
    (gravar a reserva/autorização e, na mesma transação, chamar esta função
    com `origem='aprovada', destino='executada'`) é composta pelo chamador:
    ele deve envolver a chamada a esta função e a chamada de gravação
    (`inserir_reserva`/`autorizar_visitante`) na mesma `transacao()`,
    para que as duas coisas sejam atômicas juntas.

    Retorna `True` se exatamente uma linha foi alterada (transição válida e
    aplicada). Retorna `False` se nenhuma linha casou — `id` inexistente,
    de outra sessão, ou não estava no estado `origem`. Isso cobre
    explicitamente o **reenvio de uma confirmação já respondida**: o
    chamador deve traduzir `False` em `409`, sem jamais enviar nada ao
    ADK — testes locais observaram o ADK executar a ação de novo quando
    uma aprovação já respondida era reenviada no mesmo processo; esta
    função evita isso decidindo antes de o ADK ser consultado.

    Levanta `ValueError` se `(origem, destino)` não estiver em
    `TRANSICOES_VALIDAS` — é um erro de uso do chamador, não uma condição
    de corrida, e não deve ser tratado como `409`.

    `resultado`, se informado, é gravado como JSON (tipicamente ao marcar
    `executada` ou `falhou`), para que uma reexecução da tool pelo ADK
    ("at least once") possa devolver o mesmo resultado sem gravar de novo
    (ver `obter_confirmacao`, que decodifica de volta para `dict`).
    """
    if (origem, destino) not in TRANSICOES_VALIDAS:
        raise ValueError(f"transição inválida: {origem} -> {destino}")

    campos = ["estado = ?"]
    valores: list = [destino]
    agora = _agora()
    if destino in ("aprovada", "negada"):
        campos.append("decidida_em = ?")
        valores.append(agora)
    if destino == "executada":
        campos.append("executada_em = ?")
        valores.append(agora)
    if motivo is not None:
        campos.append("motivo = ?")
        valores.append(motivo)
    if resultado is not None:
        campos.append("resultado = ?")
        valores.append(json.dumps(resultado, ensure_ascii=False, default=str))

    cur = conn.execute(
        f"UPDATE confirmacoes SET {', '.join(campos)} "
        "WHERE id = ? AND session_id = ? AND estado = ?",
        (*valores, id, session_id, origem),
    )
    return cur.rowcount == 1


def _linha_confirmacao(row: sqlite3.Row) -> dict:
    """Converte uma linha de `confirmacoes` em `dict`, decodificando
    `resultado` de volta de JSON para `dict` (ou `None` se não gravado)."""
    d = dict(row)
    if d.get("resultado"):
        d["resultado"] = json.loads(d["resultado"])
    return d


def obter_confirmacao(conn: sqlite3.Connection, *, id: str, session_id: str) -> Optional[dict]:
    row = conn.execute(
        "SELECT * FROM confirmacoes WHERE id = ? AND session_id = ?", (id, session_id)
    ).fetchone()
    return _linha_confirmacao(row) if row else None


def obter_confirmacao_por_function_call_id(
    conn: sqlite3.Connection, *, original_function_call_id: str
) -> Optional[dict]:
    row = conn.execute(
        "SELECT * FROM confirmacoes WHERE original_function_call_id = ?",
        (original_function_call_id,),
    ).fetchone()
    return _linha_confirmacao(row) if row else None


def listar_pendentes(conn: sqlite3.Connection, session_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM confirmacoes WHERE session_id = ? AND estado = 'pendente' "
        "ORDER BY criada_em",
        (session_id,),
    ).fetchall()
    return [_linha_confirmacao(r) for r in rows]


def listar_aprovadas_sem_execucao(conn: sqlite3.Connection) -> list[dict]:
    """Usado na reconciliação da subida: confirmações `aprovada` que não
    chegaram a `executada`, `falhou` nem `interrompida` — a subida as
    marca `interrompida`, sem reexecução automática."""
    rows = conn.execute("SELECT * FROM confirmacoes WHERE estado = 'aprovada'").fetchall()
    return [_linha_confirmacao(r) for r in rows]


def _agora() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()
