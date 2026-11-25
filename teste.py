from __future__ import annotations

import argparse
import logging
import os
import re
import signal
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlencode, urlparse

import requests
from pymongo import ASCENDING, MongoClient, UpdateOne
from pymongo.errors import BulkWriteError, PyMongoError

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

log = logging.getLogger("explorer")

RAIZES_PADRAO = [
    "/spaces/CCENTER/pages/750395627/2026",
    "/spaces/CCENTER/pages/561739216/2025",
]
LIMITE_POR_REQUISICAO = 100
TAMANHO_LOTE_MONGO = 500
MAX_TENTATIVAS = 6


class NaoEhConfluence(Exception):
    pass


class ErroAutenticacao(Exception):
    pass


@dataclass
class Raiz:
    page_id: str
    espaco: str | None


def parse_raiz(texto: str) -> Raiz:
    texto = texto.strip()
    if texto.isdigit():
        return Raiz(texto, None)
    m = re.search(r"/spaces/([^/]+)/pages/(\d+)", texto)
    if m:
        return Raiz(m.group(2), m.group(1))
    m = re.search(r"pageId=(\d+)", texto)
    if m:
        return Raiz(m.group(1), None)
    raise ValueError(f"Nao reconheci a pagina-raiz: {texto!r}")


class ConfluenceClient:
    def __init__(self, base_url: str, usuario: str | None, token: str,
                 modo_auth: str, pausa: float = 0.0, timeout: float = 30.0):
        self.base = base_url.rstrip("/")
        self.api = self.base + "/rest/api"
        self.host = urlparse(self.base).netloc.lower()
        self.pausa = pausa
        self.timeout = timeout
        self.requisicoes = 0

        self.sessao = requests.Session()
        self.sessao.headers.update({"Accept": "application/json"})
        if modo_auth == "basic":
            if not usuario:
                raise ValueError("CONFLUENCE_USER e obrigatorio para autenticacao basic")
            self.sessao.auth = (usuario, token)
        else:
            self.sessao.headers["Authorization"] = f"Bearer {token}"

    def _get(self, url: str, params: dict | None = None) -> dict:
        if urlparse(url).netloc.lower() != self.host:
            raise NaoEhConfluence(f"URL fora do host configurado: {url}")

        for tentativa in range(1, MAX_TENTATIVAS + 1):
            if self.pausa:
                time.sleep(self.pausa)
            try:
                resp = self.sessao.get(url, params=params, timeout=self.timeout,
                                       allow_redirects=False)
            except (requests.ConnectionError, requests.Timeout) as e:
                espera = min(2 ** tentativa, 60)
                log.warning("Falha de rede (%s). Nova tentativa em %ss", e, espera)
                time.sleep(espera)
                continue
            self.requisicoes += 1

            if resp.status_code == 429 or resp.status_code >= 500:
                espera = _retry_after(resp)
                if espera is None:
                    espera = min(2 ** tentativa, 60)
                log.warning("HTTP %s em %s. Nova tentativa em %ss",
                            resp.status_code, resp.url, espera)
                time.sleep(espera)
                continue
            if 300 <= resp.status_code < 400:
                raise NaoEhConfluence(
                    f"Redirecionado para {resp.headers.get('Location')!r} "
                    "(provavel tela de login ou URL base errada)")
            if resp.status_code in (401, 403):
                raise ErroAutenticacao(f"HTTP {resp.status_code} em {resp.url}")
            resp.raise_for_status()

            if "json" not in resp.headers.get("Content-Type", "").lower():
                raise NaoEhConfluence(
                    f"Resposta nao e JSON ({resp.headers.get('Content-Type')}) em {resp.url}")
            try:
                return resp.json()
            except ValueError as e:
                raise NaoEhConfluence(f"JSON invalido em {resp.url}") from e

        raise RuntimeError(f"Desisti apos {MAX_TENTATIVAS} tentativas: {url}")

    def paginar(self, caminho: str, params: dict):
        url = self.api + caminho
        params = dict(params)
        visitadas = set()
        while True:
            chave = url + "?" + urlencode(sorted((params or {}).items()))
            if chave in visitadas:
                log.warning("Paginacao repetida detectada, parando em %s", chave)
                return
            visitadas.add(chave)

            dados = self._get(url, params)
            if not isinstance(dados, dict) or not isinstance(dados.get("results"), list):
                raise NaoEhConfluence(f"Formato inesperado em {url}")
            resultados = dados["results"]
            yield from resultados

            proximo = (dados.get("_links") or {}).get("next")
            if proximo:
                url = proximo if proximo.startswith("http") else self.base + proximo
                params = None
            elif resultados and len(resultados) == dados.get("limit") and "start" in dados:
                params = dict(params or {})
                params["start"] = dados["start"] + len(resultados)
            else:
                return

    def pagina(self, page_id: str) -> dict:
        return self._get(f"{self.api}/content/{page_id}", {"expand": "version,space"})

    def filhas(self, page_id: str):
        yield from self.paginar(f"/content/{page_id}/child/page", {
            "expand": "version,space",
            "limit": LIMITE_POR_REQUISICAO,
        })


def _retry_after(resp: requests.Response) -> float | None:
    valor = resp.headers.get("Retry-After")
    try:
        return min(float(valor), 300) if valor else None
    except ValueError:
        return None


def parse_data(valor) -> datetime | None:
    if not isinstance(valor, str) or not valor:
        return None
    texto = valor.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(texto)
    except ValueError:
        for formato in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
            try:
                dt = datetime.strptime(valor, formato)
                break
            except ValueError:
                continue
        else:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def extrair_metadados(item: dict, base: str) -> tuple[dict | None, str | None]:
    if not isinstance(item, dict):
        return None, "item nao e objeto"
    if item.get("type") != "page":
        return None, "nao e pagina"

    page_id = str(item.get("id", ""))
    if not page_id.isdigit():
        return None, "id invalido"

    titulo = (item.get("title") or "").strip()
    if not titulo:
        return None, "sem titulo"

    versao = item.get("version") or {}
    numero = versao.get("number")
    if not isinstance(numero, int) or isinstance(numero, bool) or numero < 1:
        return None, "sem versao"

    data = parse_data(versao.get("when"))
    if data is None:
        return None, "data de modificacao invalida"

    webui = (item.get("_links") or {}).get("webui")
    if not webui:
        return None, "sem _links.webui"

    return {
        "page_id": page_id,
        "titulo": titulo,
        "versao": numero,
        "data_modificacao": data,
        "espaco": (item.get("space") or {}).get("key"),
        "url": base.rstrip("/") + webui,
        "origem": base.rstrip("/"),
    }, None


class RepositorioMongo:
    def __init__(self, colecao, dry_run: bool = False):
        self.colecao = colecao
        self.dry_run = dry_run
        self._buffer: list[UpdateOne] = []
        self.novos = 0
        self.existentes = 0
        if not dry_run:
            colecao.create_index([("page_id", ASCENDING), ("versao", ASCENDING)],
                                 unique=True, name="page_id_versao")
            colecao.create_index([("page_id", ASCENDING)], name="page_id")

    def adicionar(self, doc: dict):
        if self.dry_run:
            return
        doc = dict(doc, coletado_em=datetime.now(timezone.utc))
        self._buffer.append(UpdateOne(
            {"page_id": doc["page_id"], "versao": doc["versao"]},
            {"$setOnInsert": doc},
            upsert=True,
        ))
        if len(self._buffer) >= TAMANHO_LOTE_MONGO:
            self.descarregar()

    def descarregar(self):
        if not self._buffer:
            return
        lote, self._buffer = self._buffer, []
        try:
            r = self.colecao.bulk_write(lote, ordered=False)
            novos, existentes = r.upserted_count, r.matched_count
        except BulkWriteError as e:
            det = e.details
            erros = det.get("writeErrors", [])
            if any(err.get("code") != 11000 for err in erros):
                raise
            novos = det.get("nUpserted", 0)
            existentes = det.get("nMatched", 0) + len(erros)
        self.novos += novos
        self.existentes += existentes


@dataclass
class Resumo:
    inicio: float = field(default_factory=time.monotonic)
    raizes: list = field(default_factory=list)
    raizes_erro: list = field(default_factory=list)
    encontradas: int = 0
    rejeitadas: int = 0
    motivos_rejeicao: dict = field(default_factory=dict)
    ramos_erro: list = field(default_factory=list)
    interrompido: bool = False
    limite_atingido: bool = False

    def rejeitar(self, motivo: str):
        self.rejeitadas += 1
        self.motivos_rejeicao[motivo] = self.motivos_rejeicao.get(motivo, 0) + 1


def validar_raiz(cliente: ConfluenceClient, raiz: Raiz) -> tuple[dict, str]:
    dados = cliente.pagina(raiz.page_id)
    links = dados.get("_links") if isinstance(dados, dict) else None
    if not isinstance(links, dict):
        raise NaoEhConfluence(f"Raiz {raiz.page_id}: resposta sem _links")
    base = links.get("base", "")
    if urlparse(base).netloc.lower() != cliente.host:
        raise NaoEhConfluence(
            f"Raiz {raiz.page_id}: _links.base ({base!r}) nao corresponde ao host {cliente.host!r}")
    if dados.get("type") != "page" or str(dados.get("id")) != raiz.page_id:
        raise NaoEhConfluence(f"Raiz {raiz.page_id}: nao e uma pagina")
    espaco = (dados.get("space") or {}).get("key")
    if raiz.espaco and (espaco or "").upper() != raiz.espaco.upper():
        raise NaoEhConfluence(
            f"Raiz {raiz.page_id}: esta no espaco {espaco!r}, esperado {raiz.espaco!r}")
    return dados, base


def explorar(cliente: ConfluenceClient, repo: RepositorioMongo, raizes: list[Raiz], *,
             max_paginas: int | None = None, parar=lambda: False) -> Resumo:
    resumo = Resumo()
    visitadas: set[str] = set()

    for raiz in raizes:
        if parar() or resumo.limite_atingido:
            break
        try:
            dados_raiz, base = validar_raiz(cliente, raiz)
        except (ErroAutenticacao, requests.HTTPError) as e:
            log.error("Raiz %s ignorada: %s", raiz.page_id, e)
            resumo.raizes_erro.append((raiz.page_id, str(e)))
            continue

        titulo_raiz = dados_raiz.get("title", "")
        log.info("Raiz %s - %s (%s)", raiz.page_id, titulo_raiz,
                 (dados_raiz.get("space") or {}).get("key"))
        visitadas.add(raiz.page_id)
        antes = resumo.encontradas

        fila = deque([raiz.page_id])
        while fila:
            if parar():
                resumo.interrompido = True
                break
            pai = fila.popleft()
            try:
                for item in cliente.filhas(pai):
                    if parar():
                        resumo.interrompido = True
                        break
                    if max_paginas and resumo.encontradas >= max_paginas:
                        resumo.limite_atingido = True
                        break
                    doc, motivo = extrair_metadados(item, base)
                    if doc is None:
                        resumo.rejeitar(motivo)
                        log.debug("Rejeitado %s: %s", item.get("id"), motivo)
                        continue
                    if doc["page_id"] in visitadas:
                        continue
                    visitadas.add(doc["page_id"])
                    fila.append(doc["page_id"])

                    doc.update(pai_id=pai, raiz_id=raiz.page_id, raiz_titulo=titulo_raiz)
                    repo.adicionar(doc)
                    resumo.encontradas += 1
                    if resumo.encontradas % 200 == 0:
                        log.info("... %d paginas coletadas", resumo.encontradas)
            except (ErroAutenticacao, requests.HTTPError, RuntimeError) as e:
                log.error("Filhas de %s ignoradas: %s", pai, e)
                resumo.ramos_erro.append(pai)
            if resumo.limite_atingido:
                log.info("Limite de %d paginas atingido", max_paginas)
                break

        repo.descarregar()
        resumo.raizes.append((raiz.page_id, titulo_raiz, resumo.encontradas - antes))
        if resumo.interrompido:
            break

    repo.descarregar()
    return resumo


def imprimir_resumo(resumo: Resumo, cliente: ConfluenceClient, repo: RepositorioMongo):
    duracao = int(time.monotonic() - resumo.inicio)
    linha = "=" * 60
    status = ""
    if resumo.interrompido:
        status = "  (interrompido)"
    elif resumo.limite_atingido:
        status = "  (limite de paginas atingido)"
    print()
    print(linha)
    print(" RESUMO DO EXPLORER" + status)
    print(linha)
    print(f" Confluence ............. {cliente.base}")
    for pid, titulo, n in resumo.raizes:
        print(f" Raiz {pid} ({titulo}) ... {n} paginas")
    for pid, motivo in resumo.raizes_erro:
        print(f" Raiz {pid} ... ERRO: {motivo}")
    print(f" Paginas encontradas .... {resumo.encontradas}")
    if repo.dry_run:
        print(" Mongo .................. dry-run (nada gravado)")
    else:
        print(f" Novas no Mongo ......... {repo.novos}")
        print(f" Ja existentes .......... {repo.existentes}")
    print(f" Rejeitadas ............. {resumo.rejeitadas}")
    for motivo, n in sorted(resumo.motivos_rejeicao.items(), key=lambda x: -x[1]):
        print(f"    - {motivo}: {n}")
    if resumo.ramos_erro:
        print(f" Ramos com erro ......... {len(resumo.ramos_erro)}: "
              + ", ".join(resumo.ramos_erro[:10])
              + (" ..." if len(resumo.ramos_erro) > 10 else ""))
    print(f" Requisicoes HTTP ....... {cliente.requisicoes}")
    print(f" Tempo total ............ {duracao // 60}m{duracao % 60:02d}s")
    print(linha)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="Coleta metadados das paginas descendentes de paginas-raiz do Confluence.")
    p.add_argument("raizes", nargs="*", default=RAIZES_PADRAO,
                   help="caminho (/spaces/X/pages/ID/...), URL ou ID das paginas-raiz "
                        "(padrao: CCENTER 2026 e 2025)")
    p.add_argument("--max-paginas", type=int, help="para apos N paginas")
    p.add_argument("--pausa", type=float, default=0.0,
                   help="segundos de espera entre requisicoes (padrao 0)")
    p.add_argument("--dry-run", action="store_true", help="nao grava no Mongo")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")

    try:
        raizes = [parse_raiz(r) for r in args.raizes]
    except ValueError as e:
        log.error(str(e))
        return 2

    url = os.getenv("CONFLUENCE_URL")
    token = os.getenv("CONFLUENCE_TOKEN")
    usuario = os.getenv("CONFLUENCE_USER")
    if not url or not token:
        log.error("Defina CONFLUENCE_URL e CONFLUENCE_TOKEN (ambiente ou .env)")
        return 2
    modo = os.getenv("CONFLUENCE_AUTH") or ("basic" if usuario else "bearer")
    cliente = ConfluenceClient(url, usuario, token, modo, pausa=args.pausa)

    colecao = None
    if not args.dry_run:
        try:
            mongo = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"),
                                serverSelectionTimeoutMS=5000)
            mongo.admin.command("ping")
        except PyMongoError as e:
            log.error("Nao foi possivel conectar ao MongoDB: %s", e)
            return 2
        colecao = mongo[os.getenv("MONGO_DB", "confluence")][os.getenv("MONGO_COLLECTION", "paginas")]
    repo = RepositorioMongo(colecao, dry_run=args.dry_run)

    interromper = {"sim": False}

    def ao_ctrl_c(*_):
        if interromper["sim"]:
            raise KeyboardInterrupt
        interromper["sim"] = True
        log.warning("Interrompendo apos o item atual (Ctrl+C de novo para forcar)...")

    signal.signal(signal.SIGINT, ao_ctrl_c)

    try:
        resumo = explorar(cliente, repo, raizes, max_paginas=args.max_paginas,
                          parar=lambda: interromper["sim"])
    except NaoEhConfluence as e:
        log.error("Abortado - a origem nao parece ser o Confluence: %s", e)
        return 3

    imprimir_resumo(resumo, cliente, repo)
    return 1 if (resumo.raizes_erro or resumo.ramos_erro) else 0


if __name__ == "__main__":
    sys.exit(main())
