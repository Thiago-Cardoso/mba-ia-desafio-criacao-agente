"""Carregador somente leitura de `dados/regulamento.md`, indexado por
artigo.

A unidade de recuperação é o artigo, não o capítulo — isso mantém cada
resposta pequena e específica. Este módulo lê o arquivo uma única vez,
com `Path.read_text` (sem modo de escrita), e mantém os artigos em
memória; nunca escreve em `dados/` e nunca devolve o regulamento
inteiro ou um capítulo inteiro de uma vez.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_RE_CAPITULO = re.compile(r"^## (Cap[ií]tulo[^\n]*)$", re.MULTILINE)
_RE_ARTIGO_INICIO = re.compile(r"\*\*Art\. (\d+)[ºo°]?\.?\*\*")


@dataclass(frozen=True)
class Artigo:
    numero: str
    capitulo: str
    texto: str


class Regulamento:
    """Índice em memória do regulamento, por número de artigo."""

    def __init__(self, caminho: str | Path):
        self._caminho = Path(caminho)
        self._artigos: dict[str, Artigo] = {}
        self._carregar()

    def _carregar(self) -> None:
        texto = self._caminho.read_text(encoding="utf-8")
        partes = _RE_CAPITULO.split(texto)
        # partes[0] é o preâmbulo antes do primeiro capítulo (título do
        # documento); o restante alterna título de capítulo e corpo.
        for i in range(1, len(partes), 2):
            titulo_capitulo = partes[i].strip()
            corpo = partes[i + 1] if i + 1 < len(partes) else ""
            self._indexar_capitulo(titulo_capitulo, corpo)

    def _indexar_capitulo(self, titulo_capitulo: str, corpo: str) -> None:
        posicoes = list(_RE_ARTIGO_INICIO.finditer(corpo))
        for idx, m in enumerate(posicoes):
            numero = m.group(1)
            inicio = m.start()
            fim = posicoes[idx + 1].start() if idx + 1 < len(posicoes) else len(corpo)
            texto_artigo = corpo[inicio:fim].strip()
            self._artigos[numero] = Artigo(
                numero=numero, capitulo=titulo_capitulo, texto=texto_artigo
            )

    def quantidade_artigos(self) -> int:
        return len(self._artigos)

    def quantidade_capitulos(self) -> int:
        return len({artigo.capitulo for artigo in self._artigos.values()})

    def por_numero(self, numero: str) -> Artigo | None:
        return self._artigos.get(str(numero).strip())

    def buscar(self, termos: list[str], *, limite: int = 2) -> list[Artigo]:
        """Devolve até `limite` artigos cujo texto contém TODOS os termos
        (comparação sem diferenciar caixa), na ordem do regulamento.

        Nunca devolve mais que `limite` artigos, mesmo que mais artigos
        combinem com os termos: a tool que chama este método é a única
        barreira de quantidade, e ela depende desta garantia para nunca
        devolver o regulamento inteiro de uma vez.
        """
        termos_normalizados = [t.lower() for t in termos if t.strip()]
        if not termos_normalizados:
            return []
        encontrados: list[Artigo] = []
        for numero in sorted(self._artigos, key=int):
            artigo = self._artigos[numero]
            texto_normalizado = artigo.texto.lower()
            if all(termo in texto_normalizado for termo in termos_normalizados):
                encontrados.append(artigo)
                if len(encontrados) >= limite:
                    break
        return encontrados
