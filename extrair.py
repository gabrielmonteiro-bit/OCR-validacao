import argparse
import csv
import glob
import json
import mimetypes
import os
import re
from collections import defaultdict

KEY_FILE = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "keys-documentAI-fatura.json")
PROJECT_ID = os.getenv("DOCAI_PROJECT", "ancient-ship-487018-j0")
LOCATION = os.getenv("DOCAI_LOCATION", "us")
EXTENSOES = (".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff")

# Schema ativo de cada processador (ordem de exibição no CSV).
# None = campo simples; lista = tabela com seus subcampos ativos.
MODELOS = {
    "fatura": {
        "processador": os.getenv("DOCAI_PROCESSOR_FATURA", os.getenv("DOCAI_PROCESSOR", "3e0afbb9e70050c")),
        "versao": os.getenv("DOCAI_VERSION_FATURA", os.getenv("DOCAI_VERSION", "")),
        "pasta": "documentos",
        "cache": "respostas.json",
        "saida": "saida",
        "consolidado": "todas_faturas.csv",
        "distribuidora": True,       # identifica pela chave NF3e (guarda o texto OCR)
        "schema": {
            "account_contract_id": None,
            "connection_type": None,
            "consumption_history_table": ["history_month", "history_kwh_value"],  # history_days inativo
            "customer_cpf_cnpj": None,
            "customer_name": None,
            "due_date": None,
            "installation_id": None,
            "invoice_line_items": ["item_description", "item_quantity", "item_unit_price",
                                   "item_total_amount"],
            "rate_class": None,
            "service_address_city": None,
            "service_address_state": None,
            "service_address_street": None,
            "service_address_zip": None,
            "service_neighborhood": None,
            "total_measured_consumption": None,
        },
    },
    "rg": {
        "processador": os.getenv("DOCAI_PROCESSOR_RG", ""),
        "versao": os.getenv("DOCAI_VERSION_RG", ""),
        "pasta": "documentos_rg",
        "cache": "respostas_rg.json",
        "saida": "saida_rg",
        "consolidado": "todos_rg.csv",
        "distribuidora": False,      # não guarda o texto OCR (dados pessoais)
        "schema": {
            "nomeCompleto": None,
            "CPF": None,
            "docIdentidade": None,
            "dataDeNascimento": None,
            "dataEmissao": None,
            "validade": None,
            "CNH": None,
        },
    },
}


def colunas(m):
    tem_tabela = any(f for f in m["schema"].values())
    return (["arquivo"] + (["distribuidora"] if m["distribuidora"] else []) + ["campo"]
            + (["subcampo"] if tem_tabela else []) + ["ocorrencia", "valor_extraido",
            "valor_normalizado", "confianca", "pagina", "posicao"])


DISTRIBUIDORAS_CNPJ = {
    "15139629000194": "Neoenergia",
    "06981180000116": "Cemig Distribuição",
    "60444437000146": "Light",
    "33050196000188": "CPFL",
    "07282377000120": "Energisa Sul-Sudeste",
    "02016440000162": "CPFL",
    "02302100000106": "EDP",
    "02328280000197": "Neoenergia",
    "07282377000120": "Energisa",
    "01543032000104": "Equatorial",
    "07282377000120": "Equatorial",
    "06840748000189": "Equatorial",

}


# =============================================================== Document AI
def criar_cliente(m, nome_modelo):
    if not m["processador"]:
        raise SystemExit(f"Defina DOCAI_PROCESSOR_{nome_modelo.upper()} com o ID do processador.")
    from google.api_core.client_options import ClientOptions
    from google.cloud import documentai
    from google.oauth2 import service_account

    cred = service_account.Credentials.from_service_account_file(
        KEY_FILE, scopes=["https://www.googleapis.com/auth/cloud-platform"])
    client = documentai.DocumentProcessorServiceClient(
        credentials=cred,
        client_options=ClientOptions(api_endpoint=f"{LOCATION}-documentai.googleapis.com"))
    if m["versao"]:
        return client, client.processor_version_path(PROJECT_ID, LOCATION, m["processador"],
                                                     m["versao"])
    return client, client.processor_path(PROJECT_ID, LOCATION, m["processador"])


def _local(ent):
    """Página (1, 2...), posição 'x=0.12 y=0.34' e caixa [x0, y0, x1, y1] (0 a 1)."""
    try:
        ref = ent.page_anchor.page_refs[0]
    except (IndexError, AttributeError):
        return "", "", None
    pts = list(ref.bounding_poly.normalized_vertices)
    if not pts:
        return int(ref.page) + 1, "", None
    x0, y0 = min(p.x for p in pts), min(p.y for p in pts)
    x1, y1 = max(p.x for p in pts), max(p.y for p in pts)
    return (int(ref.page) + 1, f"x={x0:.2f} y={y0:.2f}",
            [round(x0, 4), round(y0, 4), round(x1, 4), round(y1, 4)])


def _entidade(ent):
    pagina, posicao, caixa = _local(ent)
    return {
        "tipo": ent.type_,
        "valor": ent.mention_text,
        "normalizado": ent.normalized_value.text if ent.normalized_value else "",
        "confianca": round(ent.confidence, 4),
        "pagina": pagina,
        "posicao": posicao,
        "caixa": caixa,          # usada pelo dashboard.py para recortar a fatura
        "propriedades": [_entidade(p) for p in ent.properties],
    }


def processar(client, nome_proc, caminho, m):
    from google.cloud import documentai

    mime = mimetypes.guess_type(caminho)[0] or "application/pdf"
    with open(caminho, "rb") as f:
        raw = documentai.RawDocument(content=f.read(), mime_type=mime)
    doc = client.process_document(
        request=documentai.ProcessRequest(name=nome_proc, raw_document=raw)).document
    resp = {"processador": nome_proc,
            "entidades": [_entidade(e) for e in doc.entities if e.type_ in m["schema"]]}
    if m["distribuidora"]:
        resp["texto"] = doc.text
    return resp


# =============================================================== distribuidora
def _dv_ok(ch):
    soma, peso = 0, 2
    for d in reversed(ch[:43]):
        soma += int(d) * peso
        peso = 2 if peso == 9 else peso + 1
    dv = 11 - soma % 11
    return (0 if dv >= 10 else dv) == int(ch[43])


def distribuidora(texto):
    t = re.sub(r"\s+", "", texto or "")
    for m in re.finditer(r"(?=(\d{44}))", t):
        ch = m.group(1)
        if ch[20:22] == "66" and _dv_ok(ch):        # chave de acesso NF3e válida
            c = ch[6:20]
            return DISTRIBUIDORAS_CNPJ.get(c, f"CNPJ {c[:2]}.{c[2:5]}.{c[5:8]}/{c[8:12]}-{c[12:]}")
    # chave ilegível: procura o CNPJ de uma distribuidora conhecida no texto
    so_digitos = re.sub(r"\D", "", texto or "")
    for cnpj, nome in DISTRIBUIDORAS_CNPJ.items():
        if cnpj in so_digitos:
            return nome
    return ""


# =============================================================== CSV
def _limpo(v):
    return re.sub(r"\s+", " ", str(v or "")).strip()


def linhas_do_documento(arquivo, resp, m):
    base = {"arquivo": arquivo}
    if m["distribuidora"]:
        base["distribuidora"] = distribuidora(resp.get("texto", ""))
    por_tipo = defaultdict(list)
    for e in resp.get("entidades", []):
        por_tipo[e["tipo"]].append(e)

    linhas = []
    for campo, filhos in m["schema"].items():
        ocorrencias = por_tipo.get(campo, [])
        if not ocorrencias:                       # não retornado: linha(s) vazia(s)
            for sub in (filhos or [""]):
                linhas.append({**base, "campo": campo, "ocorrencia": "",
                               **({"subcampo": sub} if filhos else {})})
            continue
        for n, e in enumerate(ocorrencias, 1):
            if filhos is None:
                linhas.append({**base, "campo": campo, "ocorrencia": n,
                               "valor_extraido": _limpo(e["valor"]),
                               "valor_normalizado": _limpo(e["normalizado"]),
                               "confianca": e["confianca"], "pagina": e["pagina"],
                               "posicao": e["posicao"]})
                continue
            props = defaultdict(list)
            for p in e.get("propriedades", []):
                props[p["tipo"]].append(p)
            for sub in filhos:
                for p in props.get(sub) or [{}]:
                    linhas.append({**base, "campo": campo, "subcampo": sub, "ocorrencia": n,
                                   "valor_extraido": _limpo(p.get("valor")),
                                   "valor_normalizado": _limpo(p.get("normalizado")),
                                   "confianca": p.get("confianca", ""),
                                   "pagina": p.get("pagina", ""), "posicao": p.get("posicao", "")})
    return linhas


def gravar(caminho, linhas, cols):
    with open(caminho, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols, delimiter=";", restval="", extrasaction="ignore")
        w.writeheader()
        w.writerows(linhas)


def gerar_csvs(respostas, m):
    cols = colunas(m)
    os.makedirs(m["saida"], exist_ok=True)
    todas = []
    for arquivo, resp in sorted(respostas.items()):
        if "erro" in resp:
            linhas = [{"arquivo": arquivo, "campo": "(erro na chamada)",
                       "valor_extraido": resp["erro"]}]
        else:
            linhas = linhas_do_documento(arquivo, resp, m)
        gravar(os.path.join(m["saida"], os.path.splitext(arquivo)[0] + ".csv"), linhas, cols)
        todas += linhas
    gravar(os.path.join(m["saida"], m["consolidado"]), todas, cols)
    return len(respostas)


# =============================================================== main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--modelo", choices=list(MODELOS), default="fatura")
    ap.add_argument("--reprocessar", action="store_true")
    ap.add_argument("--sem-api", action="store_true")
    a = ap.parse_args()
    m = MODELOS[a.modelo]

    respostas = {}
    if os.path.exists(m["cache"]) and not a.reprocessar:
        with open(m["cache"], encoding="utf-8") as f:
            respostas = json.load(f)

    if not a.sem_api:
        arquivos = sorted(p for p in glob.glob(os.path.join(m["pasta"], "*"))
                          if p.lower().endswith(EXTENSOES))
        pendentes = [p for p in arquivos
                     if "entidades" not in respostas.get(os.path.basename(p), {})]
        if pendentes:
            client, nome_proc = criar_cliente(m, a.modelo)
            print(f"Processador: {nome_proc}")
            for i, arq in enumerate(pendentes, 1):
                nome = os.path.basename(arq)
                print(f"[{i}/{len(pendentes)}] {nome}")
                try:
                    respostas[nome] = processar(client, nome_proc, arq, m)
                except Exception as e:  # noqa: BLE001
                    print(f"   falha na chamada: {e}")
                    respostas[nome] = {"erro": str(e)}
                with open(m["cache"], "w", encoding="utf-8") as f:
                    json.dump(respostas, f, ensure_ascii=False, indent=2)

    if not respostas:
        raise SystemExit(f"Nenhum documento: coloque os arquivos em '{m['pasta']}/'.")
    n = gerar_csvs(respostas, m)
    print(f"{n} documento(s) -> {m['saida']}/<documento>.csv e {m['saida']}/{m['consolidado']}")


if __name__ == "__main__":
    main()