from __future__ import annotations
"""
      href="/pages/diffpagesbyversion.action?pageId=660104132&amp;selectedPageVersions=69&amp;selectedPageVersions=70"
"""
import logging
import os
import random
import re
import signal
import sys
import time
from collections import deque
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlsplit

try:
    import dateparser
    import requests
    import truststore
    from bs4 import BeautifulSoup
    from dateparser.search import search_dates
    from dotenv import dotenv_values
    from pymongo import ASCENDING, MongoClient, UpdateOne
    from pymongo.errors import BulkWriteError, PyMongoError
except ImportError:
    sys.exit("Esqueceu das dependências. Instale com: python -m pip install -r requirements.txt")

"""
CONFLUENCE_BASE_URL=https://confluence.suaempresa.com.br
CONFLUENCE_PAT=
CONFLUENCE_CA_BUNDLE=

MONGO_URI=mongodb://localhost:27017
MONGO_DB=local
MONGO_COLLECTION=paginas

EXPLORER_RAIZES=
EXPLORER_ARQUIVO_RAIZES=raizes.txt

EXPLORER_DIAGNOSTICO=TRUE
EXPLORER_DRY_RUN=TRUE
EXPLORER_SO_ARVORE=FALSE
EXPLORER_VERBOSE=FALSE

EXPLORER_MAX_PAGINAS=200
EXPLORER_MAX_REQUISICOES=150
EXPLORER_MAX_PROFUNDIDADE=5
EXPLORER_PAUSA=5
EXPLORER_PAUSA_ENTRE_RAIZES=30
"""


log = logging.getLogger("explorer")

ARQUIVO_ENV = ".env"
ARQUIVO_RAIZES = "raizes.txt"
RAIZES_PADRAO = [
    "/spaces/CCENTER/pages/750395627/2026",
    "/spaces/CCENTER/pages/561739216/2025",
]

DIAGNOSTICO = False
DRY_RUN = False
SO_ARVORE = False
VERBOSE = False
MAX_PAGINAS = 200
MAX_REQUISICOES = 150
MAX_PROFUNDIDADE = 5
PAUSA = 5.0
PAUSA_ENTRE_RAIZES = 30.0

MONGO_URI_PADRAO = "mongodb://localhost:27017"
MONGO_DB_PADRAO = "local"
MONGO_COLLECTION_PADRAO = "paginas"
TAMANHO_LOTE_MONGO = 100

URL_ARVORE = "/plugins/pagetree/naturalchildren.action"
URL_INFO = "/pages/viewinfo.action"
URL_PAGINA = "/pages/viewpage.action"

VARIACAO_PAUSA = 0.3
FATOR_APOS_429 = 2.0
MAX_INTERVALO = 60.0
MAX_TENTATIVAS = 4
MAX_BACKOFF = 120
MAX_RETRY_AFTER = 300
TIMEOUT = (10, 60)
FALHAS_SEGUIDAS = 3

FUSO_DATAS = "America/Sao_Paulo"
CONFIG_DATAS = {
    "TIMEZONE": FUSO_DATAS,
    "TO_TIMEZONE": "UTC",
    "RETURN_AS_TIMEZONE_AWARE": True,
    "DATE_ORDER": "DMY",
}

PARADAS = {"autenticacao", "limite", "falha", "sem_permissao", "nao_encontrada", "http",
           "interrompido", "orcamento", "nao_confluence"}
PARADAS_DE_RAMO = {"sem_permissao", "nao_encontrada", "http", "falha"}


def parada(tipo, mensagem):
    return RuntimeError(tipo, mensagem)


def tipo_parada(exc):
    if isinstance(exc, RuntimeError) and len(exc.args) == 2 and exc.args[0] in PARADAS:
        return exc.args[0]
    return None


def tentar(funcao, *args):
    try:
        return funcao(*args), None
    except RuntimeError as exc:
        if tipo_parada(exc) in PARADAS_DE_RAMO:
            return None, exc
        raise


def parse_raiz(texto):
    texto = texto.strip()
    if texto.isdigit():
        return {"page_id": texto, "espaco": None}
    m = re.search(r"/spaces/([^/]+)/pages/(\d+)", texto)
    if m:
        return {"page_id": m.group(2), "espaco": m.group(1)}
    m = re.search(r"pageId=(\d+)", texto)
    if m:
        return {"page_id": m.group(1), "espaco": None}
    raise ValueError(f"Nao reconheci a pagina-pai: {texto!r}")


def ler_arquivo_raizes(caminho):
    linhas = []
    for bruta in Path(caminho).read_text(encoding="utf-8-sig").splitlines():
        texto = bruta.split(" #", 1)[0].strip()
        if texto and not texto.startswith("#"):
            linhas.append(texto)
    return linhas


def escolher_raizes(lista_env, arquivo):
    if lista_env:
        textos, origem = [t for t in re.split(r"[,;\s]+", lista_env) if t], "EXPLORER_RAIZES no .env"
    elif Path(arquivo).is_file():
        textos, origem = ler_arquivo_raizes(arquivo), arquivo
    elif arquivo != ARQUIVO_RAIZES:
        raise ValueError(f"Arquivo de paginas-pai nao encontrado: {arquivo}")
    else:
        textos, origem = RAIZES_PADRAO, "RAIZES_PADRAO"
    raizes, vistas = [], set()
    for texto in textos:
        raiz = parse_raiz(texto)
        if raiz["page_id"] not in vistas:
            vistas.add(raiz["page_id"])
            raizes.append(raiz)
    if not raizes:
        raise ValueError(f"Nenhuma pagina-pai informada em {origem}")
    return raizes, origem


def ler_numero(valores, nome, padrao, tipo):
    texto = (valores.get(nome) or "").strip()
    if not texto:
        return padrao
    try:
        numero = tipo(texto.replace(",", "."))
    except ValueError:
        sys.exit(f"{nome} no {ARQUIVO_ENV} deve ser um numero, veio '{texto}'.")
    if numero < 0:
        sys.exit(f"{nome} no {ARQUIVO_ENV} nao pode ser negativo.")
    return numero


def ler_booleano(valores, nome, padrao):
    texto = (valores.get(nome) or "").strip().upper()
    if not texto:
        return padrao
    if texto in ("TRUE", "VERDADEIRO", "SIM", "1"):
        return True
    if texto in ("FALSE", "FALSO", "NAO", "NÃO", "0"):
        return False
    sys.exit(f"{nome} no {ARQUIVO_ENV} deve ser TRUE ou FALSE, veio '{texto}'.")


def ler_config(valores):
    return {
        "raizes": (valores.get("EXPLORER_RAIZES") or "").strip(),
        "arquivo_raizes": (valores.get("EXPLORER_ARQUIVO_RAIZES") or "").strip() or ARQUIVO_RAIZES,
        "diagnostico": ler_booleano(valores, "EXPLORER_DIAGNOSTICO", DIAGNOSTICO),
        "dry_run": ler_booleano(valores, "EXPLORER_DRY_RUN", DRY_RUN),
        "so_arvore": ler_booleano(valores, "EXPLORER_SO_ARVORE", SO_ARVORE),
        "verbose": ler_booleano(valores, "EXPLORER_VERBOSE", VERBOSE),
        "max_paginas": ler_numero(valores, "EXPLORER_MAX_PAGINAS", MAX_PAGINAS, int),
        "max_requisicoes": ler_numero(valores, "EXPLORER_MAX_REQUISICOES", MAX_REQUISICOES, int),
        "max_profundidade": ler_numero(valores, "EXPLORER_MAX_PROFUNDIDADE", MAX_PROFUNDIDADE, int),
        "pausa": ler_numero(valores, "EXPLORER_PAUSA", PAUSA, float),
        "pausa_raiz": ler_numero(valores, "EXPLORER_PAUSA_ENTRE_RAIZES", PAUSA_ENTRE_RAIZES, float),
        "mongo_uri": (valores.get("MONGO_URI") or "").strip() or MONGO_URI_PADRAO,
        "mongo_db": (valores.get("MONGO_DB") or "").strip() or MONGO_DB_PADRAO,
        "mongo_collection": (valores.get("MONGO_COLLECTION") or "").strip() or MONGO_COLLECTION_PADRAO,
    }


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


def criar_sessao(pat, ca_bundle):
    truststore.inject_into_ssl()
    sessao = requests.Session()
    sessao.trust_env = False
    sessao.headers.update({
        "Authorization": f"Bearer {pat}",
        "X-Atlassian-Token": "no-check",
        "Accept": "text/html, */*;q=0.8",
    })
    sessao.verify = ca_bundle or True
    return sessao


def explicar_erro_tls(exc):
    texto = str(exc).lower()
    if "hostname" in texto or "match" in texto or "not valid for" in texto:
        return "o certificado do servidor é de outro endereço. Confira CONFLUENCE_BASE_URL."
    if "expired" in texto:
        return "o certificado do servidor está vencido. Avise a equipe que mantém o Confluence."
    return ("o certificado do servidor não é confiável neste computador. Se o navegador abre o Confluence "
            "sem aviso, exporte a CA da empresa em formato PEM (Base-64) e informe o arquivo em CONFLUENCE_CA_BUNDLE.")


def eh_login(url):
    return bool(url) and urlsplit(url).path.lower().rstrip("/").endswith(
        ("/login.action", "/dologin.action", "/login.jsp"))


def eh_tela_login(html):
    return (re.search(r'type=["\']?password', html, re.I) is not None
            and re.search(r"os_username|login", html, re.I) is not None)


def criar_cliente(base_url, pat, ca_bundle, intervalo, max_requisicoes, parar):
    return {
        "base": base_url.rstrip("/"),
        "host": urlsplit(base_url).netloc.lower(),
        "sessao": criar_sessao(pat, ca_bundle),
        "intervalo": max(0.0, intervalo),
        "proxima": 0.0,
        "max_requisicoes": max_requisicoes,
        "parar": parar,
        "requisicoes": 0,
        "respostas_429": 0,
        "arvores_truncadas": 0,
        "limite_informado": False,
    }


def dormir(cli, segundos):
    fim = time.monotonic() + max(0.0, segundos)
    while True:
        if cli["parar"]():
            raise parada("interrompido", "interrompido pelo usuario")
        restante = fim - time.monotonic()
        if restante <= 0:
            return
        time.sleep(min(restante, 0.5))


def agendar(cli, segundos=0.0):
    pausa = cli["intervalo"] * (1 + random.uniform(0, VARIACAO_PAUSA))
    cli["proxima"] = time.monotonic() + max(pausa, segundos)


def descansar(cli, segundos):
    cli["proxima"] = max(cli["proxima"], time.monotonic() + segundos)


def desacelerar(cli, ritmo):
    novo = min(max(cli["intervalo"] * FATOR_APOS_429, ritmo or 0.0), MAX_INTERVALO)
    if novo > cli["intervalo"]:
        log.warning("Reduzindo o ritmo para 1 requisicao a cada %.1fs", novo)
        cli["intervalo"] = novo


def cabecalhos_limite(headers):
    return {k: v for k, v in headers.items() if k.lower().startswith(("retry-after", "x-ratelimit"))}


def ritmo_servidor(headers):
    try:
        intervalo = float(headers["X-RateLimit-Interval-Seconds"])
        reposicao = float(headers["X-RateLimit-FillRate"])
    except (KeyError, ValueError):
        return None
    return intervalo / reposicao if intervalo > 0 and reposicao > 0 else None


def restantes_servidor(headers):
    try:
        return int(headers["X-RateLimit-Remaining"])
    except (KeyError, ValueError):
        return None


def bloqueado_servidor(headers):
    for nome in ("X-RateLimit-Limit", "X-RateLimit-FillRate"):
        try:
            if float(headers[nome]) <= 0:
                return True
        except (KeyError, ValueError):
            continue
    return False


def segundos_retry_after(resp):
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


def espera_apos_sucesso(cli, headers):
    if not cli["limite_informado"] and cabecalhos_limite(headers):
        cli["limite_informado"] = True
        log.info("Limite informado pelo Confluence: %s", cabecalhos_limite(headers))
    ritmo = ritmo_servidor(headers)
    restantes = restantes_servidor(headers)
    if ritmo and restantes is not None and restantes <= 2:
        return ritmo
    return 0.0


def pedir_html(cli, caminho, params):
    url = cli["base"] + caminho
    problema = ""
    for tentativa in range(1, MAX_TENTATIVAS + 1):
        if cli["max_requisicoes"] and cli["requisicoes"] >= cli["max_requisicoes"]:
            raise parada("orcamento", f"limite de {cli['max_requisicoes']} requisicoes atingido")
        dormir(cli, cli["proxima"] - time.monotonic())
        backoff = min(max(cli["intervalo"], 1.0) * 2 ** tentativa, MAX_BACKOFF)

        try:
            resp = cli["sessao"].get(url, params=params, timeout=TIMEOUT, allow_redirects=False,
                                     verify=cli["sessao"].verify)
        except requests.exceptions.SSLError:
            cli["requisicoes"] += 1
            raise
        except (requests.ConnectionError, requests.Timeout) as exc:
            cli["requisicoes"] += 1
            problema = "tempo esgotado" if isinstance(exc, requests.Timeout) else "falha de conexao"
            log.warning("%s (tentativa %d/%d). Nova tentativa em %.0fs", problema, tentativa, MAX_TENTATIVAS, backoff)
            log.debug("Detalhe: %s", exc)
            agendar(cli, backoff)
            continue
        cli["requisicoes"] += 1

        with resp:
            codigo = resp.status_code
            if codigo == 429:
                cli["respostas_429"] += 1
                if cli["respostas_429"] == 1:
                    log.warning("Primeiro HTTP 429. Cabecalhos de limite: %s",
                                cabecalhos_limite(resp.headers) or "nenhum (pode vir de um proxy/WAF)")
                if bloqueado_servidor(resp.headers):
                    raise parada("limite", "o Confluence esta bloqueando o seu usuario (cota zero)")
                espera = segundos_retry_after(resp)
                desacelerar(cli, ritmo_servidor(resp.headers))
                espera = backoff if espera is None else espera
                if espera > MAX_RETRY_AFTER:
                    raise parada("limite", f"o Confluence pediu {espera:.0f}s de espera (HTTP 429)")
                problema = "HTTP 429"
                log.warning("HTTP 429 (tentativa %d/%d). Aguardando %.0fs",
                            tentativa, MAX_TENTATIVAS, max(espera, cli["intervalo"]))
                agendar(cli, espera)
                continue

            if codigo == 408 or codigo >= 500:
                espera = segundos_retry_after(resp) or backoff
                if espera > MAX_RETRY_AFTER:
                    raise parada("falha", f"o Confluence pediu {espera:.0f}s de espera (HTTP {codigo})")
                problema = f"HTTP {codigo}"
                log.warning("%s em %s (tentativa %d/%d). Nova tentativa em %.0fs",
                            problema, caminho, tentativa, MAX_TENTATIVAS, max(espera, backoff))
                agendar(cli, max(espera, backoff))
                continue

            agendar(cli, espera_apos_sucesso(cli, resp.headers))

            if 300 <= codigo < 400:
                destino = resp.headers.get("Location")
                if eh_login(destino):
                    raise parada("autenticacao", "PAT recusado (redirecionado para a tela de login)")
                raise parada("nao_confluence", f"redirecionado para {destino!r} (confira CONFLUENCE_BASE_URL)")
            if codigo == 401:
                raise parada("autenticacao", "PAT recusado (HTTP 401)")
            if codigo == 403:
                raise parada("sem_permissao", f"sem permissao (HTTP 403) em {caminho}?pageId={params.get('pageId')}")
            if codigo == 404:
                raise parada("nao_encontrada", f"nao encontrada (HTTP 404): pageId={params.get('pageId')}")
            if codigo >= 400:
                raise parada("http", f"HTTP {codigo} em {caminho}?pageId={params.get('pageId')}")
            if "html" not in resp.headers.get("Content-Type", "").lower():
                raise parada("nao_confluence", f"resposta nao e HTML ({resp.headers.get('Content-Type')})")
            html = resp.text
            if eh_tela_login(html):
                raise parada("autenticacao", "PAT recusado (o Confluence devolveu a tela de login)")
            return html

    if problema == "HTTP 429":
        raise parada("limite", f"HTTP 429 persistente apos {MAX_TENTATIVAS} tentativas")
    raise parada("falha", f"sem resposta valida apos {MAX_TENTATIVAS} tentativas ({problema}) em {caminho}")


def ler_arvore(html, page_id):
    sopa = BeautifulSoup(html, "html.parser")
    filhas = []
    for span in sopa.find_all(id=re.compile(r"^childrenspan\d+-\d+$")):
        pid = re.match(r"childrenspan(\d+)-", span["id"]).group(1)
        if pid == page_id:
            continue
        link = span.find("a")
        titulo = (link or span).get_text(" ", strip=True)
        filhas.append({"id": pid, "titulo": titulo})
    com_filhas = {re.match(r"plusminus(\d+)-", a["id"]).group(1)
                  for a in sopa.find_all(id=re.compile(r"^plusminus\d+-\d+$"))}
    truncada = any(
        "showall" in re.sub(r"[-_\s]", "", " ".join(el.get("class") or []) + (el.get("href") or "")).lower()
        for el in sopa.find_all(["a", "span", "li", "div"])
    )
    return {
        "filhas": filhas,
        "com_filhas": com_filhas,
        "marcadores": bool(com_filhas) or bool(sopa.select(".no-children")),
        "truncada": truncada,
    }


def ler_info(html):
    sopa = BeautifulSoup(html, "html.parser")
    meta = {m["name"][4:]: m.get("content") or ""
            for m in sopa.find_all("meta", attrs={"name": re.compile(r"^ajs-")})}
    versoes = [int(n) for a in sopa.find_all("a", href=True)
               for n in re.findall(r"(?:selectedPageVersions|originalVersion|revisedVersion|pageVersion)=(\d+)",
                                   a["href"])]
    datas_iso = [el["datetime"] for el in sopa.find_all(attrs={"datetime": True})]
    datas_texto = [el.get_text(" ", strip=True) for el in sopa.select(".last-modified")]
    datas_texto += [el.get_text(" ", strip=True) for el in sopa.select(".page-metadata-modification-info")]
    return {"meta": meta, "versoes": versoes, "datas_iso": datas_iso,
            "datas_texto": [t for t in datas_texto if t]}


def parse_data_iso(valor):
    if not isinstance(valor, str) or not valor.strip():
        return None
    try:
        dt = datetime.fromisoformat(valor.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_data_tela(texto):
    if not texto:
        return None
    dt = dateparser.parse(texto, languages=["pt", "en"], settings=CONFIG_DATAS)
    if dt is None:
        achadas = search_dates(texto, languages=["pt", "en"], settings=CONFIG_DATAS)
        dt = achadas[-1][1] if achadas else None
    return dt.astimezone(timezone.utc) if dt else None


def listar_filhas(cli, page_id):
    html = pedir_html(cli, URL_ARVORE, {
        "decorator": "none", "excerpt": "false", "sort": "position", "reverse": "false",
        "disableLinks": "false", "expandCurrent": "false", "hasRoot": "true",
        "pageId": page_id, "treeId": "0", "startDepth": "0", "mobile": "false",
    })
    return ler_arvore(html, page_id)


def obter_info(cli, page_id):
    info = ler_info(pedir_html(cli, URL_INFO, {"pageId": page_id}))
    if not info["meta"].get("page-id"):
        raise parada("nao_confluence", f"a tela de informacoes de {page_id} nao tem os metadados do Confluence")
    return info


def montar_doc(cli, page_id, titulo_arvore, info):
    doc = {
        "page_id": page_id,
        "titulo": (titulo_arvore or "").strip(),
        "versao": None,
        "data_modificacao": None,
        "data_modificacao_texto": None,
        "espaco": None,
        "url": f"{cli['base']}{URL_PAGINA}?pageId={page_id}",
        "origem": cli["base"],
        "coletado_via": "web",
    }
    if info is None:
        return doc, []

    faltando = []
    meta = info["meta"]
    doc["titulo"] = (meta.get("page-title") or doc["titulo"]).strip()
    doc["espaco"] = meta.get("space-key") or None

    versao = meta.get("page-version") or meta.get("version-number") or ""
    if versao.isdigit():
        doc["versao"] = int(versao)
    elif info["versoes"]:
        doc["versao"] = max(info["versoes"])
    else:
        faltando.append("versao")

    for candidato in info["datas_iso"]:
        data = parse_data_iso(candidato)
        if data:
            doc["data_modificacao"], doc["data_modificacao_texto"] = data, candidato
            break
    else:
        for candidato in info["datas_texto"]:
            data = parse_data_tela(candidato)
            if data:
                doc["data_modificacao"], doc["data_modificacao_texto"] = data, candidato
                break
        else:
            faltando.append("data")
            doc["data_modificacao_texto"] = (info["datas_texto"] or [None])[0]

    if not doc["titulo"]:
        faltando.append("titulo")
    return doc, faltando


def validar_raiz(cli, raiz):
    info = obter_info(cli, raiz["page_id"])
    pid = info["meta"].get("page-id")
    if pid != raiz["page_id"]:
        raise parada("nao_confluence", f"pagina-pai {raiz['page_id']}: o Confluence mostrou a pagina {pid}")
    espaco = info["meta"].get("space-key")
    if raiz["espaco"] and (espaco or "").upper() != raiz["espaco"].upper():
        raise parada("nao_confluence",
                     f"pagina-pai {raiz['page_id']}: esta no espaco {espaco!r}, esperado {raiz['espaco']!r}")
    return info, (info["meta"].get("page-title") or "").strip()


def criar_repo(colecao, dry_run):
    if not dry_run:
        colecao.create_index([("page_id", ASCENDING), ("versao", ASCENDING)], unique=True, name="page_id_versao")
        colecao.create_index([("page_id", ASCENDING)], name="page_id")
    return {"colecao": colecao, "dry_run": dry_run, "buffer": [], "novos": 0, "existentes": 0}


def descarregar(repo):
    if not repo["buffer"]:
        return
    lote, repo["buffer"] = repo["buffer"], []
    try:
        r = repo["colecao"].bulk_write(lote, ordered=False)
        novos, existentes = r.upserted_count, r.matched_count
    except BulkWriteError as exc:
        det = exc.details
        erros = det.get("writeErrors", [])
        if any(err.get("code") != 11000 for err in erros):
            raise
        novos = det.get("nUpserted", 0)
        existentes = det.get("nMatched", 0) + len(erros)
    repo["novos"] += novos
    repo["existentes"] += existentes


def adicionar(repo, doc):
    if repo["dry_run"]:
        return
    doc = dict(doc, coletado_em=datetime.now(timezone.utc))
    repo["buffer"].append(UpdateOne({"page_id": doc["page_id"], "versao": doc["versao"]},
                                    {"$setOnInsert": doc}, upsert=True))
    if len(repo["buffer"]) >= TAMANHO_LOTE_MONGO:
        descarregar(repo)


def novo_resumo():
    return {
        "inicio": time.monotonic(),
        "raizes": [],
        "raizes_erro": [],
        "encontradas": 0,
        "sem_campo": {},
        "ignoradas": {},
        "ramos_erro": [],
        "profundidade_excedida": 0,
        "falhas_seguidas": 0,
        "interrompido": False,
        "limite": "",
        "abortado": None,
    }


def contar(dicionario, chave):
    dicionario[chave] = dicionario.get(chave, 0) + 1


def contar_falha(resumo, exc):
    if tipo_parada(exc) != "falha":
        return False
    resumo["falhas_seguidas"] += 1
    if resumo["falhas_seguidas"] < FALHAS_SEGUIDAS:
        return False
    resumo["abortado"] = (6, f"{resumo['falhas_seguidas']} falhas seguidas sem resposta valida do Confluence. "
                             "Confira a VPN, a URL e se o servidor esta no ar.")
    return True


def registrar(cli, repo, resumo, page_id, titulo, info, **extra):
    doc, faltando = montar_doc(cli, page_id, titulo, info)
    for campo in faltando:
        contar(resumo["sem_campo"], campo)
    doc.update(extra)
    adicionar(repo, doc)
    resumo["encontradas"] += 1
    if resumo["encontradas"] % 50 == 0:
        log.info("... %d paginas coletadas (%d requisicoes)", resumo["encontradas"], cli["requisicoes"])


def explorar(cli, repo, raizes, max_paginas, max_profundidade, pausa_raiz, so_arvore):
    resumo = novo_resumo()
    visitadas = set()
    parar = cli["parar"]
    try:
        for indice, raiz in enumerate(raizes):
            if parar() or resumo["limite"] or resumo["abortado"]:
                break
            pid_raiz = raiz["page_id"]
            if pid_raiz in visitadas:
                log.info("Pagina-pai %s ja foi coberta por outra pagina-pai; pulando", pid_raiz)
                resumo["raizes"].append((pid_raiz, "(ja coberta)", 0))
                continue
            if indice and pausa_raiz:
                log.info("Pausa de %.0fs antes da proxima pagina-pai", pausa_raiz)
                descansar(cli, pausa_raiz)

            resultado, erro = tentar(validar_raiz, cli, raiz)
            if erro:
                log.error("Pagina-pai %s ignorada: %s", pid_raiz, erro.args[1])
                resumo["raizes_erro"].append((pid_raiz, erro.args[1]))
                if contar_falha(resumo, erro):
                    break
                continue
            resumo["falhas_seguidas"] = 0
            info_raiz, titulo_raiz = resultado
            log.info("Pagina-pai %s - %s (%s)", pid_raiz, titulo_raiz, info_raiz["meta"].get("space-key"))
            visitadas.add(pid_raiz)
            antes = resumo["encontradas"]
            registrar(cli, repo, resumo, pid_raiz, titulo_raiz, info_raiz,
                      pai_id=None, raiz_id=pid_raiz, raiz_titulo=titulo_raiz, profundidade=0)

            fila = deque([(pid_raiz, 0)])
            while fila and not resumo["abortado"] and not resumo["limite"]:
                if parar():
                    resumo["interrompido"] = True
                    break
                pai, nivel = fila.popleft()
                arvore, erro = tentar(listar_filhas, cli, pai)
                if erro:
                    log.error("Filhas de %s ignoradas: %s", pai, erro.args[1])
                    resumo["ramos_erro"].append(pai)
                    contar_falha(resumo, erro)
                    continue
                resumo["falhas_seguidas"] = 0
                if arvore["truncada"]:
                    cli["arvores_truncadas"] += 1
                    log.warning("A arvore de %s mostrou 'mostrar todas': algumas filhas podem faltar", pai)

                for filha in arvore["filhas"]:
                    if parar():
                        resumo["interrompido"] = True
                        break
                    if max_paginas and resumo["encontradas"] >= max_paginas:
                        resumo["limite"] = f"limite de {max_paginas} paginas"
                        break
                    pid = filha["id"]
                    if pid in visitadas:
                        continue
                    visitadas.add(pid)

                    info = None
                    if not so_arvore:
                        info, erro = tentar(obter_info, cli, pid)
                        if erro:
                            log.warning("Pagina %s ignorada: %s", pid, erro.args[1])
                            contar(resumo["ignoradas"], tipo_parada(erro))
                            if tipo_parada(erro) == "falha":
                                resumo["ramos_erro"].append(pid)
                            if contar_falha(resumo, erro):
                                break
                            continue
                        resumo["falhas_seguidas"] = 0

                    registrar(cli, repo, resumo, pid, filha["titulo"], info,
                              pai_id=pai, raiz_id=pid_raiz, raiz_titulo=titulo_raiz, profundidade=nivel + 1)

                    if pid in arvore["com_filhas"] or not arvore["marcadores"]:
                        if not max_profundidade or nivel + 1 < max_profundidade:
                            fila.append((pid, nivel + 1))
                        else:
                            resumo["profundidade_excedida"] += 1

            descarregar(repo)
            resumo["raizes"].append((pid_raiz, titulo_raiz, resumo["encontradas"] - antes))
            if resumo["interrompido"]:
                break

    except RuntimeError as exc:
        tipo = tipo_parada(exc)
        if tipo is None:
            raise
        mensagem = exc.args[1]
        if tipo == "interrompido":
            resumo["interrompido"] = True
        elif tipo == "orcamento":
            resumo["limite"] = mensagem
        elif tipo == "limite":
            resumo["abortado"] = (5, f"Limite de requisicoes do Confluence: {mensagem}. "
                                     "Espere alguns minutos e aumente EXPLORER_PAUSA no .env.")
        elif tipo == "autenticacao":
            resumo["abortado"] = (4, f"{mensagem}. Gere um novo PAT, atualize o {ARQUIVO_ENV} e rode de novo.")
        else:
            resumo["abortado"] = (3, f"Resposta inesperada do Confluence: {mensagem}")
    except requests.exceptions.SSLError as exc:
        log.debug("Erro TLS: %s", exc)
        resumo["abortado"] = (4, "Certificado TLS recusado: " + explicar_erro_tls(exc))
    finally:
        descarregar(repo)
    return resumo


def imprimir_resumo(resumo, cli, repo):
    duracao = int(time.monotonic() - resumo["inicio"])
    linha = "=" * 60
    if resumo["abortado"]:
        status = "  (ABORTADO)"
    elif resumo["interrompido"]:
        status = "  (interrompido)"
    elif resumo["limite"]:
        status = f"  ({resumo['limite']})"
    else:
        status = ""
    print()
    print(linha)
    print(" RESUMO DO EXPLORER" + status)
    print(linha)
    print(f" Confluence ............. {cli['base']}")
    for pid, titulo, n in resumo["raizes"]:
        print(f" Pagina-pai {pid} ({titulo}) ... {n} paginas")
    for pid, motivo in resumo["raizes_erro"]:
        print(f" Pagina-pai {pid} ... ERRO: {motivo}")
    print(f" Paginas encontradas .... {resumo['encontradas']}")
    if repo["dry_run"]:
        print(" Mongo .................. dry-run (nada gravado)")
    else:
        print(f" Mongo .................. {repo['colecao'].database.name}.{repo['colecao'].name}")
        print(f" Novas no Mongo ......... {repo['novos']}")
        print(f" Ja existentes .......... {repo['existentes']}")
    for campo, n in resumo["sem_campo"].items():
        print(f" Gravadas sem {campo} ...... {n}")
    for motivo, n in resumo["ignoradas"].items():
        print(f" Ignoradas ({motivo}) ... {n}")
    if resumo["ramos_erro"]:
        print(f" Ramos com erro ......... {len(resumo['ramos_erro'])}: "
              + ", ".join(resumo["ramos_erro"][:10]) + (" ..." if len(resumo["ramos_erro"]) > 10 else ""))
    if resumo["profundidade_excedida"]:
        print(f" Paginas no limite de profundidade ... {resumo['profundidade_excedida']}")
    if cli["arvores_truncadas"]:
        print(f" Arvores incompletas ('mostrar todas') ... {cli['arvores_truncadas']}")
    print(f" Requisicoes HTTP ....... {cli['requisicoes']} ({cli['respostas_429']} com HTTP 429)")
    print(f" Ritmo final ............ 1 requisicao a cada {cli['intervalo']:.1f}s")
    print(f" Tempo total ............ {duracao // 60}m{duracao % 60:02d}s")
    if resumo["abortado"]:
        print(f" Motivo ................. {resumo['abortado'][1]}")
    print(linha)


def diagnosticar(cli, page_id):
    try:
        info = obter_info(cli, page_id)
        print(f"Informacoes da pagina {page_id}:")
        for chave in ("page-id", "page-title", "space-key", "page-version", "parent-page-id"):
            print(f"  ajs-{chave}: {info['meta'].get(chave)!r}")
        print(f"  versoes nos links: {sorted(set(info['versoes']))[-5:]}")
        print(f"  datas (datetime): {info['datas_iso'][:3]}")
        print(f"  datas (texto): {info['datas_texto'][:3]}")
        doc, faltando = montar_doc(cli, page_id, "", info)
        print(f"  => versao={doc['versao']} data={doc['data_modificacao']} espaco={doc['espaco']} "
              f"faltando={faltando or 'nada'}")

        arvore = listar_filhas(cli, page_id)
        print(f"\nArvore de {page_id}: {len(arvore['filhas'])} filhas "
              f"(sabe quem tem filhas: {'sim' if arvore['marcadores'] else 'nao'}; "
              f"incompleta: {'sim' if arvore['truncada'] else 'nao'})")
        for filha in arvore["filhas"][:10]:
            marca = "+" if filha["id"] in arvore["com_filhas"] else " "
            print(f"  {marca} {filha['id']}  {filha['titulo']}")
        if len(arvore["filhas"]) > 10:
            print(f"  ... e mais {len(arvore['filhas']) - 10}")
        print(f"\n{cli['requisicoes']} requisicoes, {cli['respostas_429']} com HTTP 429.")
        return 0
    except RuntimeError as exc:
        if tipo_parada(exc) is None:
            raise
        print(f"Falhou ({exc.args[0]}): {exc.args[1]}")
        return 1
    except requests.exceptions.SSLError as exc:
        print("Certificado TLS recusado:", explicar_erro_tls(exc))
        return 4


def main():
    base_url, pat, ca_bundle, valores = ler_env()
    cfg = ler_config(valores)

    logging.basicConfig(level=logging.DEBUG if cfg["verbose"] else logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    try:
        raizes, origem = escolher_raizes(cfg["raizes"], cfg["arquivo_raizes"])
    except ValueError as exc:
        log.error(str(exc))
        return 2

    interromper = {"sim": False}
    cli = criar_cliente(base_url, pat, ca_bundle, cfg["pausa"], cfg["max_requisicoes"] or None,
                        lambda: interromper["sim"])

    if cfg["diagnostico"]:
        return diagnosticar(cli, raizes[0]["page_id"])

    log.info("%d pagina(s)-pai (de %s): %s", len(raizes), origem, ", ".join(r["page_id"] for r in raizes))
    log.info("Limites: %s paginas, %s requisicoes, profundidade %s, pausa %.1fs, pausa entre pais %.0fs%s%s",
             cfg["max_paginas"] or "sem limite", cfg["max_requisicoes"] or "sem limite",
             cfg["max_profundidade"] or "sem limite", cfg["pausa"], cfg["pausa_raiz"],
             " | so arvore" if cfg["so_arvore"] else "", " | dry-run" if cfg["dry_run"] else "")

    colecao = None
    if not cfg["dry_run"]:
        try:
            mongo = MongoClient(cfg["mongo_uri"], serverSelectionTimeoutMS=5000)
            mongo.admin.command("ping")
        except PyMongoError as exc:
            log.error("Nao foi possivel conectar ao MongoDB: %s", exc)
            return 2
        colecao = mongo[cfg["mongo_db"]][cfg["mongo_collection"]]
    repo = criar_repo(colecao, cfg["dry_run"])

    def ao_ctrl_c(*_):
        if interromper["sim"]:
            raise KeyboardInterrupt
        interromper["sim"] = True
        log.warning("Interrompendo (Ctrl+C de novo para forcar)...")

    signal.signal(signal.SIGINT, ao_ctrl_c)
    try:
        resumo = explorar(cli, repo, raizes, cfg["max_paginas"] or None, cfg["max_profundidade"] or None,
                          cfg["pausa_raiz"], cfg["so_arvore"])
    except KeyboardInterrupt:
        log.error("Interrompido a forca.")
        return 130

    imprimir_resumo(resumo, cli, repo)
    if resumo["abortado"]:
        log.error(resumo["abortado"][1])
        return resumo["abortado"][0]
    return 1 if (resumo["raizes_erro"] or resumo["ramos_erro"]) else 0


if __name__ == "__main__":
    sys.exit(main())
