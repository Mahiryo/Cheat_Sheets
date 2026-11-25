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
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlencode, urlparse

try:
    import requests
    import truststore
    from dotenv import dotenv_values
    from pymongo import ASCENDING, MongoClient, UpdateOne
    from pymongo.errors import BulkWriteError, PyMongoError
except ImportError:
    sys.exit("Esqueceu das dependências. Instale com: python -m pip install -r requirements.txt")

log = logging.getLogger("explorer")

ARQUIVO_ENV = ".env"
RAIZES_PADRAO = [
    "/spaces/CCENTER/pages/750395627/2026",
    "/spaces/CCENTER/pages/561739216/2025",
]
MONGO_DB_PADRAO = "local"
MONGO_COLLECTION_PADRAO = "paginas"
TAMANHO_LOTE_MONGO = 500

# --- ritmo e limites de requisicao
LIMITE_POR_REQUISICAO = 100     # itens por chamada (menos chamadas = menos consumo do limite)
INTERVALO_MINIMO = 1.0          # segundos entre requisicoes (padrao de --pausa)
MAX_TENTATIVAS = 6              # tentativas por requisicao
MAX_RETRY_AFTER = 300           # se o servidor pedir mais que isso, aborta
TIMEOUT = (10, 60)              # conexao, leitura

# --- protecoes contra loop e falha em cascata
FALHAS_SEGUIDAS = 3             # disjuntor: ramos seguidos sem resposta valida
MAX_PAGINAS_POR_LISTAGEM = 500  # paginas de resultado por listagem de filhas (500 x 100 = 50 mil filhas)
MAX_PROFUNDIDADE = 50           # niveis abaixo da raiz


# --------------------------------------------------------------------------- erros

class NaoEhConfluence(Exception):
    pass


class ErroAutenticacao(Exception):
    """PAT recusado (HTTP 401 ou tela de login). Aborta a execucao."""


class SemPermissao(Exception):
    """HTTP 403: o PAT e valido, mas nao le aquela pagina. So o ramo e ignorado."""


class LimiteExcedido(Exception):
    """HTTP 429 persistente ou espera longa demais. Aborta: as proximas chamadas tambem falhariam."""


class FalhaServidor(Exception):
    """Sem resposta valida apos todas as tentativas (rede/5xx). Conta para o disjuntor."""


class Interrompido(Exception):
    """Ctrl+C durante uma espera."""


class OrcamentoEsgotado(Exception):
    """--max-requisicoes atingido."""


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


# --------------------------------------------------------------------------- autenticacao (igual ao crawler)

def ler_env():
    valores = {**dotenv_values(ARQUIVO_ENV, interpolate=False), **os.environ}
    base_url = (valores.get("CONFLUENCE_BASE_URL") or "").strip().rstrip("/")
    pat = (valores.get("CONFLUENCE_PAT") or "").strip()
    ca_bundle = (valores.get("CONFLUENCE_CA_BUNDLE") or "").strip()
    if not base_url.startswith("https://"):
        sys.exit(f"Defina CONFLUENCE_BASE_URL (https://...) em {ARQUIVO_ENV}.")
    if not pat or any(c.isspace() for c in pat):
        sys.exit(f"Defina CONFLUENCE_PAT em {ARQUIVO_ENV}.")
    if ca_bundle and not Path(ca_bundle).is_file():
        sys.exit(f"CONFLUENCE_CA_BUNDLE não encontrado: {ca_bundle}")
    return base_url, pat, ca_bundle, valores


def criar_sessao(pat: str, ca_bundle: str) -> requests.Session:
    truststore.inject_into_ssl()
    sessao = requests.Session()
    sessao.trust_env = False
    sessao.headers.update({
        "Authorization": f"Bearer {pat}",
        "X-Atlassian-Token": "no-check",
        "Accept": "application/json",
    })
    sessao.verify = ca_bundle or True
    return sessao


def explicar_erro_tls(exc) -> str:
    texto = str(exc).lower()
    if "hostname" in texto or "match" in texto or "not valid for" in texto:
        return "o certificado do servidor é de outro endereço. Confira CONFLUENCE_BASE_URL."
    if "expired" in texto:
        return "o certificado do servidor está vencido. Avise a equipe que mantém o Confluence."
    return ("o certificado do servidor não é confiável neste computador. Se o navegador abre o Confluence "
            "sem aviso, exporte a CA da empresa em formato PEM (Base-64) e informe o arquivo em CONFLUENCE_CA_BUNDLE.")


def eh_login(url: str | None) -> bool:
    return bool(url) and urlparse(url).path.lower().rstrip("/").endswith(
        ("/login.action", "/dologin.action", "/login.jsp"))


# --------------------------------------------------------------------------- limites do servidor

def segundos_retry_after(resp: requests.Response) -> float | None:
    """Retry-After em segundos ou data HTTP; sem ele, usa o ritmo de reposicao do token bucket."""
    valor = resp.headers.get("Retry-After", "").strip()
    if valor:
        try:
            return max(0.0, float(valor))
        except ValueError:
            pass
        try:
            return max(0.0, parsedate_to_datetime(valor).timestamp() - time.time())
        except (TypeError, ValueError):
            pass
    return ritmo_servidor(resp.headers)


def ritmo_servidor(headers) -> float | None:
    """Segundos para o Confluence repor 1 requisicao (X-RateLimit-Interval-Seconds / X-RateLimit-FillRate)."""
    try:
        intervalo = float(headers["X-RateLimit-Interval-Seconds"])
        reposicao = float(headers["X-RateLimit-FillRate"])
    except (KeyError, ValueError):
        return None
    return intervalo / reposicao if intervalo > 0 and reposicao > 0 else None


def restantes_servidor(headers) -> int | None:
    try:
        return int(headers["X-RateLimit-Remaining"])
    except (KeyError, ValueError):
        return None


def cabecalhos_limite(headers) -> dict:
    return {k: v for k, v in headers.items() if k.lower().startswith(("retry-after", "x-ratelimit"))}


# --------------------------------------------------------------------------- cliente

class ConfluenceClient:
    def __init__(self, base_url: str, pat: str, ca_bundle: str = "", *,
                 intervalo: float = INTERVALO_MINIMO, max_requisicoes: int | None = None,
                 parar=lambda: False):
        self.base = base_url.rstrip("/")
        self.api = self.base + "/rest/api"
        self.host = urlparse(self.base).netloc.lower()
        self.sessao = criar_sessao(pat, ca_bundle)
        self.parar = parar
        self.max_requisicoes = max_requisicoes

        self.intervalo = max(0.0, intervalo)  # ritmo atual; sobe se o servidor pedir
        self._proxima = 0.0
        self._limite_informado = False

        self.requisicoes = 0
        self.respostas_429 = 0
        self.listagens_truncadas = 0

    # ---- ritmo

    def _dormir(self, segundos: float):
        """Espera em fatias curtas para o Ctrl+C funcionar mesmo durante esperas longas."""
        fim = time.monotonic() + max(0.0, segundos)
        while True:
            if self.parar():
                raise Interrompido()
            restante = fim - time.monotonic()
            if restante <= 0:
                return
            time.sleep(min(restante, 0.5))

    def _agendar(self, segundos: float = 0.0):
        self._proxima = time.monotonic() + max(self.intervalo, segundos)

    def _ajustar_ritmo(self, headers, apos_429: bool = False) -> float:
        """Devolve quanto esperar antes da proxima chamada, segundo os cabecalhos do servidor."""
        if not self._limite_informado and cabecalhos_limite(headers):
            self._limite_informado = True
            log.info("Limite do Confluence: %s", cabecalhos_limite(headers))
        ritmo = ritmo_servidor(headers)
        if ritmo is None:
            return 0.0
        if apos_429 and ritmo > self.intervalo:
            # O balde de tokens esvaziou: daqui em diante, anda no ritmo em que o servidor repoe.
            log.warning("Reduzindo o ritmo para 1 requisicao a cada %.1fs (limite do servidor)", ritmo)
            self.intervalo = ritmo
        restantes = restantes_servidor(headers)
        if restantes is not None and restantes <= 2:
            return ritmo
        return 0.0

    # ---- requisicao

    def _get(self, url: str, params: dict | None = None) -> dict:
        if urlparse(url).netloc.lower() != self.host:
            raise NaoEhConfluence(f"URL fora do host configurado: {url}")

        problema = ""
        for tentativa in range(1, MAX_TENTATIVAS + 1):
            if self.max_requisicoes and self.requisicoes >= self.max_requisicoes:
                raise OrcamentoEsgotado(f"limite de {self.max_requisicoes} requisicoes atingido")
            self._dormir(self._proxima - time.monotonic())
            backoff = min(max(self.intervalo, 1.0) * 2 ** (tentativa - 1), 60)

            try:
                resp = self.sessao.get(url, params=params, timeout=TIMEOUT,
                                       allow_redirects=False, verify=self.sessao.verify)
            except requests.exceptions.SSLError:
                self.requisicoes += 1
                raise
            except (requests.ConnectionError, requests.Timeout) as e:
                self.requisicoes += 1
                problema = "tempo esgotado" if isinstance(e, requests.Timeout) else "falha de conexao"
                log.warning("%s (tentativa %d/%d). Nova tentativa em %.0fs",
                            problema, tentativa, MAX_TENTATIVAS, backoff)
                log.debug("Detalhe: %s", e)
                self._agendar(backoff)
                continue
            self.requisicoes += 1

            with resp:
                if resp.status_code == 429:
                    self.respostas_429 += 1
                    espera = segundos_retry_after(resp)
                    self._ajustar_ritmo(resp.headers, apos_429=True)
                    log.debug("Cabecalhos do 429: %s", cabecalhos_limite(resp.headers))
                    if espera is None:
                        espera = backoff
                    if espera > MAX_RETRY_AFTER:
                        raise LimiteExcedido(
                            f"o Confluence pediu {espera:.0f}s de espera (HTTP 429)")
                    problema = "HTTP 429"
                    log.warning("HTTP 429 (tentativa %d/%d). Aguardando %.0fs",
                                tentativa, MAX_TENTATIVAS, max(espera, self.intervalo))
                    self._agendar(espera)
                    continue

                if resp.status_code == 408 or resp.status_code >= 500:
                    espera = segundos_retry_after(resp) or backoff
                    if espera > MAX_RETRY_AFTER:
                        raise FalhaServidor(
                            f"o Confluence pediu {espera:.0f}s de espera (HTTP {resp.status_code})")
                    problema = f"HTTP {resp.status_code}"
                    log.warning("%s em %s (tentativa %d/%d). Nova tentativa em %.0fs",
                                problema, resp.url, tentativa, MAX_TENTATIVAS, espera)
                    self._agendar(max(espera, backoff))
                    continue

                self._agendar(self._ajustar_ritmo(resp.headers))

                if 300 <= resp.status_code < 400:
                    destino = resp.headers.get("Location")
                    if eh_login(destino):
                        raise ErroAutenticacao("PAT recusado (redirecionado para a tela de login)")
                    raise NaoEhConfluence(f"Redirecionado para {destino!r} (provavel URL base errada)")
                if resp.status_code == 401:
                    raise ErroAutenticacao(f"PAT recusado (HTTP 401 em {resp.url})")
                if resp.status_code == 403:
                    raise SemPermissao(f"Sem permissao (HTTP 403 em {resp.url})")
                resp.raise_for_status()

                if "json" not in resp.headers.get("Content-Type", "").lower():
                    raise NaoEhConfluence(
                        f"Resposta nao e JSON ({resp.headers.get('Content-Type')}) em {resp.url}")
                try:
                    return resp.json()
                except ValueError as e:
                    raise NaoEhConfluence(f"JSON invalido em {resp.url}") from e

        if problema == "HTTP 429":
            raise LimiteExcedido(f"HTTP 429 persistente apos {MAX_TENTATIVAS} tentativas")
        raise FalhaServidor(f"sem resposta valida apos {MAX_TENTATIVAS} tentativas ({problema}): {url}")

    # ---- paginacao com protecao contra loop

    def paginar(self, caminho: str, params: dict):
        url = self.api + caminho
        params = dict(params)
        chaves_vistas: set[str] = set()
        ids_vistos: set[str] = set()
        ultimo_start = None

        for pagina in range(1, MAX_PAGINAS_POR_LISTAGEM + 2):
            if pagina > MAX_PAGINAS_POR_LISTAGEM:
                self.listagens_truncadas += 1
                log.warning("Listagem %s passou de %d paginas de resultado; parando",
                            caminho, MAX_PAGINAS_POR_LISTAGEM)
                return

            chave = url + "?" + urlencode(sorted((params or {}).items()))
            if chave in chaves_vistas:
                self.listagens_truncadas += 1
                log.warning("Paginacao repetida detectada, parando em %s", chave)
                return
            chaves_vistas.add(chave)

            dados = self._get(url, params)
            if not isinstance(dados, dict) or not isinstance(dados.get("results"), list):
                raise NaoEhConfluence(f"Formato inesperado em {url}")
            resultados = dados["results"]

            ids = {str(r.get("id")) for r in resultados if isinstance(r, dict)}
            novos = ids - ids_vistos
            ids_vistos |= ids
            yield from resultados

            if resultados and not novos:
                # O servidor devolveu de novo os mesmos itens: ignorou o "start" ou o "next" anda em circulo.
                self.listagens_truncadas += 1
                log.warning("Pagina de resultados sem itens novos em %s; parando", caminho)
                return

            proximo = (dados.get("_links") or {}).get("next")
            if proximo:
                url = proximo if proximo.startswith("http") else self.base + proximo
                params = None
            elif resultados and len(resultados) == dados.get("limit") and isinstance(dados.get("start"), int):
                novo_start = dados["start"] + len(resultados)
                if ultimo_start is not None and novo_start <= ultimo_start:
                    self.listagens_truncadas += 1
                    log.warning("Paginacao nao avancou em %s; parando", caminho)
                    return
                ultimo_start = novo_start
                params = dict(params or {})
                params["start"] = novo_start
            else:
                return

    def pagina(self, page_id: str) -> dict:
        return self._get(f"{self.api}/content/{page_id}", {"expand": "version,space"})

    def filhas(self, page_id: str):
        yield from self.paginar(f"/content/{page_id}/child/page", {
            "expand": "version,space",
            "limit": LIMITE_POR_REQUISICAO,
        })


# --------------------------------------------------------------------------- metadados

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


# --------------------------------------------------------------------------- mongo

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


# --------------------------------------------------------------------------- exploracao

@dataclass
class Resumo:
    inicio: float = field(default_factory=time.monotonic)
    raizes: list = field(default_factory=list)
    raizes_erro: list = field(default_factory=list)
    encontradas: int = 0
    rejeitadas: int = 0
    motivos_rejeicao: dict = field(default_factory=dict)
    ramos_erro: list = field(default_factory=list)
    profundidade_excedida: int = 0
    interrompido: bool = False
    limite_atingido: str = ""
    abortado: tuple | None = None  # (codigo de saida, mensagem)

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


class Disjuntor:
    """Abre apos N falhas seguidas de rede/servidor; qualquer sucesso fecha de novo."""

    def __init__(self, limite: int):
        self.limite = limite
        self.seguidas = 0

    def sucesso(self):
        self.seguidas = 0

    def falha(self) -> bool:
        self.seguidas += 1
        return self.seguidas >= self.limite


def explorar(cliente: ConfluenceClient, repo: RepositorioMongo, raizes: list[Raiz], *,
             max_paginas: int | None = None, max_profundidade: int = MAX_PROFUNDIDADE,
             parar=lambda: False) -> Resumo:
    resumo = Resumo()
    visitadas: set[str] = set()
    disjuntor = Disjuntor(FALHAS_SEGUIDAS)

    def abrir_disjuntor():
        resumo.abortado = (6, f"{disjuntor.seguidas} falhas seguidas sem resposta valida do Confluence. "
                              "Confira a VPN, a URL e se o servidor esta no ar.")

    try:
        for raiz in raizes:
            if parar() or resumo.limite_atingido or resumo.abortado:
                break
            try:
                dados_raiz, base = validar_raiz(cliente, raiz)
                disjuntor.sucesso()
            except (SemPermissao, requests.HTTPError) as e:
                log.error("Raiz %s ignorada: %s", raiz.page_id, e)
                resumo.raizes_erro.append((raiz.page_id, str(e)))
                continue
            except FalhaServidor as e:
                log.error("Raiz %s ignorada: %s", raiz.page_id, e)
                resumo.raizes_erro.append((raiz.page_id, str(e)))
                if disjuntor.falha():
                    abrir_disjuntor()
                    break
                continue

            titulo_raiz = dados_raiz.get("title", "")
            log.info("Raiz %s - %s (%s)", raiz.page_id, titulo_raiz,
                     (dados_raiz.get("space") or {}).get("key"))
            visitadas.add(raiz.page_id)
            antes = resumo.encontradas

            fila = deque([(raiz.page_id, 0)])
            while fila and not resumo.abortado:
                if parar():
                    resumo.interrompido = True
                    break
                pai, nivel = fila.popleft()
                try:
                    for item in cliente.filhas(pai):
                        if parar():
                            resumo.interrompido = True
                            break
                        if max_paginas and resumo.encontradas >= max_paginas:
                            resumo.limite_atingido = f"limite de {max_paginas} paginas"
                            break
                        doc, motivo = extrair_metadados(item, base)
                        if doc is None:
                            resumo.rejeitar(motivo)
                            log.debug("Rejeitado %s: %s", item.get("id"), motivo)
                            continue
                        if doc["page_id"] in visitadas:
                            continue
                        visitadas.add(doc["page_id"])

                        if nivel + 1 < max_profundidade:
                            fila.append((doc["page_id"], nivel + 1))
                        else:
                            resumo.profundidade_excedida += 1
                            log.warning("Pagina %s no nivel %d: filhas nao serao exploradas",
                                        doc["page_id"], nivel + 1)

                        doc.update(pai_id=pai, raiz_id=raiz.page_id, raiz_titulo=titulo_raiz,
                                   profundidade=nivel + 1)
                        repo.adicionar(doc)
                        resumo.encontradas += 1
                        if resumo.encontradas % 200 == 0:
                            log.info("... %d paginas coletadas (%d requisicoes)",
                                     resumo.encontradas, cliente.requisicoes)
                    disjuntor.sucesso()
                except (SemPermissao, requests.HTTPError) as e:
                    log.error("Filhas de %s ignoradas: %s", pai, e)
                    resumo.ramos_erro.append(pai)
                except FalhaServidor as e:
                    log.error("Filhas de %s ignoradas: %s", pai, e)
                    resumo.ramos_erro.append(pai)
                    if disjuntor.falha():
                        abrir_disjuntor()
                if resumo.limite_atingido:
                    break

            repo.descarregar()
            resumo.raizes.append((raiz.page_id, titulo_raiz, resumo.encontradas - antes))
            if resumo.interrompido:
                break

    except Interrompido:
        resumo.interrompido = True
    except OrcamentoEsgotado as e:
        resumo.limite_atingido = str(e)
    except LimiteExcedido as e:
        resumo.abortado = (5, f"Limite de requisicoes do Confluence: {e}. Espere alguns minutos, "
                              "confira se outro processo usa o mesmo PAT e rode de novo com --pausa maior.")
    except ErroAutenticacao as e:
        resumo.abortado = (4, f"{e}. Gere um novo PAT, atualize o {ARQUIVO_ENV} e rode de novo.")
    except requests.exceptions.SSLError as e:
        log.debug("Erro TLS: %s", e)
        resumo.abortado = (4, "Certificado TLS recusado: " + explicar_erro_tls(e))
    except NaoEhConfluence as e:
        resumo.abortado = (3, f"A origem nao parece ser o Confluence: {e}")
    finally:
        repo.descarregar()  # grava o que ja foi coletado, mesmo abortando
    return resumo


def imprimir_resumo(resumo: Resumo, cliente: ConfluenceClient, repo: RepositorioMongo):
    duracao = int(time.monotonic() - resumo.inicio)
    linha = "=" * 60
    status = ""
    if resumo.abortado:
        status = "  (ABORTADO)"
    elif resumo.interrompido:
        status = "  (interrompido)"
    elif resumo.limite_atingido:
        status = f"  ({resumo.limite_atingido})"
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
        print(f" Mongo .................. {repo.colecao.database.name}.{repo.colecao.name}")
        print(f" Novas no Mongo ......... {repo.novos}")
        print(f" Ja existentes .......... {repo.existentes}")
    print(f" Rejeitadas ............. {resumo.rejeitadas}")
    for motivo, n in sorted(resumo.motivos_rejeicao.items(), key=lambda x: -x[1]):
        print(f"    - {motivo}: {n}")
    if resumo.ramos_erro:
        print(f" Ramos com erro ......... {len(resumo.ramos_erro)}: "
              + ", ".join(resumo.ramos_erro[:10])
              + (" ..." if len(resumo.ramos_erro) > 10 else ""))
    if resumo.profundidade_excedida:
        print(f" Paginas no limite de profundidade ... {resumo.profundidade_excedida}")
    if cliente.listagens_truncadas:
        print(f" Listagens interrompidas (anti-loop) . {cliente.listagens_truncadas}")
    print(f" Requisicoes HTTP ....... {cliente.requisicoes} ({cliente.respostas_429} com HTTP 429)")
    print(f" Ritmo final ............ 1 requisicao a cada {cliente.intervalo:.1f}s")
    print(f" Tempo total ............ {duracao // 60}m{duracao % 60:02d}s")
    if resumo.abortado:
        print(f" Motivo ................. {resumo.abortado[1]}")
    print(linha)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="Coleta metadados das paginas descendentes de paginas-raiz do Confluence.")
    p.add_argument("raizes", nargs="*", default=RAIZES_PADRAO,
                   help="caminho (/spaces/X/pages/ID/...), URL ou ID das paginas-raiz "
                        "(padrao: CCENTER 2026 e 2025)")
    p.add_argument("--max-paginas", type=int, help="para apos N paginas coletadas")
    p.add_argument("--max-requisicoes", type=int, help="para apos N requisicoes HTTP")
    p.add_argument("--max-profundidade", type=int, default=MAX_PROFUNDIDADE,
                   help=f"niveis abaixo da raiz a explorar (padrao {MAX_PROFUNDIDADE})")
    p.add_argument("--pausa", type=float, default=INTERVALO_MINIMO,
                   help=f"segundos minimos entre requisicoes (padrao {INTERVALO_MINIMO}); "
                        "sobe sozinho se o Confluence pedir")
    p.add_argument("--dry-run", action="store_true", help="nao grava no Mongo")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    try:
        raizes = [parse_raiz(r) for r in args.raizes]
    except ValueError as e:
        log.error(str(e))
        return 2

    base_url, pat, ca_bundle, env = ler_env()

    interromper = {"sim": False}

    def ao_ctrl_c(*_):
        if interromper["sim"]:
            raise KeyboardInterrupt
        interromper["sim"] = True
        log.warning("Interrompendo (Ctrl+C de novo para forcar)...")

    cliente = ConfluenceClient(base_url, pat, ca_bundle, intervalo=args.pausa,
                               max_requisicoes=args.max_requisicoes,
                               parar=lambda: interromper["sim"])

    colecao = None
    if not args.dry_run:
        try:
            mongo = MongoClient(env.get("MONGO_URI") or "mongodb://localhost:27017",
                                serverSelectionTimeoutMS=5000)
            mongo.admin.command("ping")
        except PyMongoError as e:
            log.error("Nao foi possivel conectar ao MongoDB: %s", e)
            return 2
        colecao = mongo[env.get("MONGO_DB") or MONGO_DB_PADRAO][
            env.get("MONGO_COLLECTION") or MONGO_COLLECTION_PADRAO]
    repo = RepositorioMongo(colecao, dry_run=args.dry_run)

    signal.signal(signal.SIGINT, ao_ctrl_c)

    try:
        resumo = explorar(cliente, repo, raizes, max_paginas=args.max_paginas,
                          max_profundidade=args.max_profundidade,
                          parar=lambda: interromper["sim"])
    except KeyboardInterrupt:
        log.error("Interrompido a forca.")
        return 130

    imprimir_resumo(resumo, cliente, repo)
    if resumo.abortado:
        log.error(resumo.abortado[1])
        return resumo.abortado[0]
    return 1 if (resumo.raizes_erro or resumo.ramos_erro) else 0


if __name__ == "__main__":
    sys.exit(main())
