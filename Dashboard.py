import argparse
import csv
import io
import json
import os
import re
import unicodedata
from urllib.parse import quote
from datetime import datetime

from extrair import MODELOS, distribuidora

M = MODELOS["fatura"]
SAIDA = "dashboard_faturas"
REVISAO = "revisao_faturas.csv"

# Rótulos que indicam que a fatura tem um código de cliente separado da UC.
ROTULOS_COD_CLIENTE = re.compile(
    r"conta\s*contrato|c[óo]d(?:igo|\.)?\s*(?:do\s*)?cliente|seu\s*c[óo]digo|"
    r"parceiro\s*de\s*neg[óo]cio|n[ºo°]\.?\s*do\s*cliente", re.I)

MESES = {m: i for i, m in enumerate(
    ["JAN", "FEV", "MAR", "ABR", "MAI", "JUN", "JUL", "AGO", "SET", "OUT", "NOV", "DEZ"], 1)}

CHAVE = [  # id, rótulo, campo no schema
    ("uc", "UC", "installation_id"),
    ("titular", "Titular", "customer_name"),
    ("consumo", "Consumo (kWh)", "consumption_history_table.history_kwh_value"),
    ("cod_cliente", "Cód. cliente", "account_contract_id"),
]


# =============================================================== utilitários
def _norm(s):
    s = "".join(c for c in unicodedata.normalize("NFD", s or "") if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9*]", "", s.lower())


def _limpo(v):
    return re.sub(r"\s+", " ", str(v or "")).strip()


def no_texto(valor, texto):
    """True/False se o valor aparece no texto OCR; None se não há texto salvo."""
    if not texto:
        return None
    alvo = _norm(valor)
    return bool(alvo) and alvo in _norm(texto)


def numero(v):
    s = re.sub(r"[^0-9,.\-]", "", v or "")
    if not s:
        return None
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"-?\d{1,3}(\.\d{3})+", s):
        s = s.replace(".", "")
    try:
        return float(s)
    except ValueError:
        return None


def mes_ordem(txt):
    """'OUT/26', 'SET26', 'JAN 2024', '01/2024' -> 202601 (para achar o mês mais recente)."""
    t = _sem_acento(txt).upper()
    m = re.search(r"([A-Z]{3})\W*(\d{2,4})", t)
    if m and m.group(1) in MESES:
        ano = int(m.group(2))
        return (ano + 2000 if ano < 100 else ano) * 100 + MESES[m.group(1)]
    m = re.search(r"\b(\d{1,2})\W(\d{2,4})\b", t)
    if m and 1 <= int(m.group(1)) <= 12:
        ano = int(m.group(2))
        return (ano + 2000 if ano < 100 else ano) * 100 + int(m.group(1))
    return None


def data_ordem(txt):
    """'19/10/2026' ou '2026-10-19' -> 202610."""
    m = re.search(r"\d{1,2}/(\d{1,2})/(\d{4})", txt or "")
    if m:
        return int(m.group(2)) * 100 + int(m.group(1))
    m = re.search(r"(\d{4})-(\d{2})-\d{2}", txt or "")
    return int(m.group(1)) * 100 + int(m.group(2)) if m else None


def _sem_acento(s):
    return "".join(c for c in unicodedata.normalize("NFD", s or "") if unicodedata.category(c) != "Mn")


def _uniao(caixas):
    cs = [c for c in caixas if c]
    if not cs:
        return None
    return [min(c[0] for c in cs), min(c[1] for c in cs), max(c[2] for c in cs), max(c[3] for c in cs)]


# =============================================================== recortes
class Recortador:
    def __init__(self, pasta_docs, pasta_saida, ativo=True):
        self.pasta_docs, self.saida, self.cache = pasta_docs, pasta_saida, {}
        self.ok = False
        if ativo:
            try:
                import pymupdf  # noqa: F401
                from PIL import Image  # noqa: F401
                self.ok = True
            except ImportError:
                print("Sem pymupdf/Pillow: dashboard sem recortes (pip install pymupdf pillow)")
        os.makedirs(os.path.join(pasta_saida, "recortes"), exist_ok=True)

    def _pagina(self, arquivo, pagina):
        chave = (arquivo, pagina)
        if chave not in self.cache:
            import pymupdf
            from PIL import Image
            self.cache.clear()                      # guarda só a página atual na memória
            doc = pymupdf.open(os.path.join(self.pasta_docs, arquivo))
            pix = doc[pagina - 1].get_pixmap(dpi=130)
            self.cache[chave] = Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
        return self.cache[chave]

    def recortar(self, arquivo, pagina, caixa, nome):
        if not (self.ok and caixa and pagina):
            return ""
        try:
            img = self._pagina(arquivo, int(pagina))
        except Exception:  # noqa: BLE001 - arquivo ausente/corrompido
            return ""
        W, H = img.size
        x0, y0, x1, y1 = caixa
        mx, my = 0.10, 0.025                        # margem para ver o rótulo ao lado
        box = (max(0, int((x0 - mx) * W)), max(0, int((y0 - my) * H)),
               min(W, int((x1 + mx) * W)), min(H, int((y1 + my) * H)))
        if box[2] - box[0] < 20 or box[3] - box[1] < 10:
            return ""
        rec = img.crop(box)
        # contorno do trecho lido pelo modelo
        from PIL import ImageDraw
        d = ImageDraw.Draw(rec)
        d.rectangle([int(x0 * W) - box[0] - 2, int(y0 * H) - box[1] - 2,
                     int(x1 * W) - box[0] + 2, int(y1 * H) - box[1] + 2], outline=(208, 59, 59), width=2)
        if rec.width > 720:
            rec = rec.resize((720, int(rec.height * 720 / rec.width)))
        rel = f"recortes/{nome}.jpg"
        rec.save(os.path.join(self.saida, rel), quality=82)
        return rel


# =============================================================== montagem dos dados
def melhor(ocorrencias):
    return max(ocorrencias, key=lambda e: e.get("confianca") or 0) if ocorrencias else None


def montar_doc(arquivo, resp, rec, revisoes):
    texto = resp.get("texto", "")
    por_tipo = {}
    for e in resp.get("entidades", []):
        por_tipo.setdefault(e["tipo"], []).append(e)
    base = os.path.splitext(arquivo)[0]
    d = {"arquivo": arquivo, "distribuidora": distribuidora(texto) or "(não identificada)",
         "tem_texto": bool(texto), "campos": {}, "preenchidos": {},
         "link": quote(os.path.relpath(os.path.join(M["pasta"], arquivo), SAIDA).replace(os.sep, "/")),
         "existe": os.path.exists(os.path.join(M["pasta"], arquivo))}

    # ---------- campos simples principais
    for cid, _, campo in CHAVE:
        if cid == "consumo":
            continue
        ocs = [e for e in por_tipo.get(campo, []) if _limpo(e.get("valor"))]
        b = melhor(ocs)
        c = {"valor": _limpo(b["valor"]) if b else "", "normalizado": _limpo(b.get("normalizado")) if b else "",
             "confianca": b.get("confianca") if b else None, "pagina": b.get("pagina") if b else "",
             "posicao": b.get("posicao") if b else "",
             "ocorrencias": [_limpo(e["valor"]) for e in ocs],
             "no_texto": no_texto(b["valor"], texto) if b else None,
             "recorte": rec.recortar(arquivo, b.get("pagina"), b.get("caixa"), f"{base}_{cid}") if b else ""}
        if cid == "cod_cliente":
            m = ROTULOS_COD_CLIENTE.search(texto or "")
            c["rotulo"] = m.group(0) if m else ""
            c["esperado"] = bool(m) if texto else None
        d["campos"][cid] = c

    # ---------- consumo = history_kwh_value do mês mais recente
    linhas = []
    for t in por_tipo.get("consumption_history_table", []):
        props = {}
        for p in t.get("propriedades", []):
            props.setdefault(p["tipo"], p)
        mes, kwh = props.get("history_month", {}), props.get("history_kwh_value", {})
        linhas.append({"mes": _limpo(mes.get("valor")), "ordem": mes_ordem(mes.get("valor", "")),
                       "kwh": _limpo(kwh.get("valor")), "kwh_ent": kwh, "mes_ent": mes})
    com_kwh = [l for l in linhas if l["kwh"]]
    # mês mais recente que não seja posterior ao vencimento (protege contra ano lido
    # errado, ex.: "JUL 28" no lugar de "JUL 25")
    venc = melhor([e for e in por_tipo.get("due_date", []) if _limpo(e.get("valor"))])
    limite = data_ordem(venc["valor"] if venc else "") or int(datetime.now().strftime("%Y%m"))
    datadas = [l for l in com_kwh if l["ordem"] and l["ordem"] <= limite]
    ref = max(datadas, key=lambda l: l["ordem"]) if datadas else (com_kwh[0] if com_kwh else None)
    total = melhor([e for e in por_tipo.get("total_measured_consumption", []) if _limpo(e.get("valor"))])
    total_v = _limpo(total["valor"]) if total else ""
    bate = None
    if ref and total_v:
        a, b = numero(ref["kwh"]), numero(total_v)
        if a is not None and b is not None:
            bate = abs(a - b) <= max(1.0, 0.005 * abs(b))
    kwh_e = ref["kwh_ent"] if ref else {}
    d["campos"]["consumo"] = {
        "valor": ref["kwh"] if ref else "", "normalizado": _limpo(kwh_e.get("normalizado")),
        "confianca": kwh_e.get("confianca"), "pagina": kwh_e.get("pagina", ""),
        "posicao": kwh_e.get("posicao", ""), "mes": ref["mes"] if ref else "",
        "n_meses": len(linhas), "historico": [[l["mes"], l["kwh"]] for l in linhas],
        "total_medido": total_v, "bate_total": bate,
        "ocorrencias": [ref["kwh"]] if ref else [],
        "no_texto": no_texto(ref["kwh"], texto) if ref else None,
        "recorte": rec.recortar(arquivo, kwh_e.get("pagina") or (ref["mes_ent"].get("pagina") if ref else None),
                                _uniao([kwh_e.get("caixa"), ref["mes_ent"].get("caixa") if ref else None]),
                                f"{base}_consumo") if ref else "",
    }

    # ---------- revisão manual
    for cid in d["campos"]:
        d["campos"][cid]["revisao"] = revisoes.get((arquivo, cid), "")

    # ---------- preenchimento de todos os campos (seção bônus)
    for campo, filhos in M["schema"].items():
        ocs = por_tipo.get(campo, [])
        if filhos is None:
            d["preenchidos"][campo] = int(any(_limpo(e.get("valor")) for e in ocs))
        else:
            d["preenchidos"][campo] = len(ocs)
            for f in filhos:
                d["preenchidos"][f"{campo}.{f}"] = sum(
                    any(_limpo(p.get("valor")) for p in e.get("propriedades", []) if p["tipo"] == f)
                    for e in ocs)
    return d


def carregar_revisoes():
    rev = {}
    if os.path.exists(REVISAO):
        with open(REVISAO, encoding="utf-8-sig") as f:
            for r in csv.DictReader(f, delimiter=";"):
                if r.get("avaliacao") in ("correto", "incorreto"):
                    rev[(r["arquivo"], r["campo"])] = r["avaliacao"]
    return rev


# =============================================================== HTML
HTML = r"""<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Desempenho do extrator de faturas</title>
<style>
.viz-root{color-scheme:light;--surface:#fcfcfb;--panel:#f4f3f0;--line:#e2e1dc;--t1:#0b0b0b;--t2:#52514e;--t3:#6f6e69;
--accent:#2a78d6;--track:#cde2fb;--good:#0ca30c;--warn:#fab219;--crit:#d03b3b;
--h0:#f4f3f0;--h1:#cde2fb;--h2:#9ec5f4;--h3:#6da7ec;--h4:#3987e5;--h5:#1c5cab;--hi:#fff}
@media (prefers-color-scheme:dark){.viz-root{color-scheme:dark;--surface:#1a1a19;--panel:#232321;--line:#383835;
--t1:#f0efec;--t2:#c3c2b7;--t3:#9a998f;--accent:#3987e5;--track:#104281;
--h0:#232321;--h1:#104281;--h2:#184f95;--h3:#256abf;--h4:#3987e5;--h5:#86b6ef;--hi:#0b0b0b}}
body{margin:0;background:var(--surface)}
.viz-root{font:14px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;color:var(--t1);background:var(--surface);
max-width:1240px;margin:0 auto;padding:24px 24px 64px}
h1{font-size:21px;margin:0 0 2px}h2{font-size:16px;margin:34px 0 4px}
.sub,.nota{color:var(--t2);margin:0}.nota{font-size:12.5px;color:var(--t3);margin:2px 0 10px}
.filtros{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin:18px 0 6px;padding:10px 12px;background:var(--panel);border-radius:10px}
.filtros label{font-size:12.5px;color:var(--t2);display:flex;gap:6px;align-items:center}
select,button{font:inherit;font-size:13px;color:var(--t1);background:var(--surface);border:1px solid var(--line);border-radius:7px;padding:5px 9px}
button{cursor:pointer}button:hover{border-color:var(--accent)}
.cont{margin-left:auto;color:var(--t2);font-size:12.5px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(215px,1fr));gap:12px;margin-top:12px}
.kpi{background:var(--panel);border-radius:10px;padding:13px 15px}
.kpi .l{font-size:13px;color:var(--t2);font-weight:600}
.kpi .v{font-size:28px;font-weight:600;font-variant-numeric:tabular-nums;margin:2px 0 0}
.kpi .s{font-size:12.5px;color:var(--t2)}
.meter{height:6px;border-radius:3px;background:var(--track);margin:8px 0 9px;overflow:hidden}
.meter i{display:block;height:100%;background:var(--accent);border-radius:3px}
.kpi ul{margin:0;padding:0;list-style:none;font-size:12.5px;color:var(--t2)}
.kpi li{display:flex;justify-content:space-between;gap:8px;padding:2px 0;border-top:1px solid var(--line)}
.kpi li b{color:var(--t1);font-weight:600;font-variant-numeric:tabular-nums}
table{border-collapse:collapse;width:100%;font-size:13px;font-variant-numeric:tabular-nums}
th,td{padding:7px 8px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
th{font-size:12px;color:var(--t2);font-weight:600}
td.n,th.n{text-align:right}
.scroll{overflow-x:auto}
.hm td.c{text-align:center;border:2px solid var(--surface);border-radius:5px;min-width:96px}
.h0{background:var(--h0)}.h1{background:var(--h1)}.h2{background:var(--h2)}.h3{background:var(--h3)}
.h4{background:var(--h4);color:var(--hi)}.h5{background:var(--h5);color:var(--hi)}
.leg{display:flex;gap:6px;align-items:center;color:var(--t2);font-size:12px;margin:4px 0 8px}
.leg i{display:inline-block;width:22px;height:11px;border-radius:3px;outline:1px solid var(--line)}
.mono{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12.5px}
.chip{display:inline-flex;gap:4px;align-items:center;font-size:11.5px;color:var(--t2);white-space:nowrap;margin-top:2px}
.chip:before{content:"";width:8px;height:8px;border-radius:50%;background:var(--c)}
.ok{--c:var(--good)}.no{--c:var(--crit)}.vz{--c:var(--warn)}.na{--c:var(--line)}
tr.doc{cursor:pointer}tr.doc:hover td{background:var(--panel)}
tr.det>td{background:var(--panel);padding:12px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(270px,1fr));gap:12px}
.card{background:var(--surface);border:1px solid var(--line);border-radius:9px;padding:10px 12px}
.card h3{margin:0 0 6px;font-size:13px}
.card img{max-width:100%;border:1px solid var(--line);border-radius:5px;display:block;margin:4px 0 8px}
.card dl{display:grid;grid-template-columns:auto 1fr;gap:2px 10px;margin:0;font-size:12.5px}
.card dt{color:var(--t3)}.card dd{margin:0;word-break:break-word}
.rev{display:flex;gap:6px;margin-top:8px}.rev button.sel-ok{border-color:var(--good);box-shadow:inset 0 0 0 1px var(--good)}
.rev button.sel-no{border-color:var(--crit);box-shadow:inset 0 0 0 1px var(--crit)}
.bar{height:8px;background:var(--track);border-radius:4px;min-width:120px}.bar i{display:block;height:100%;background:var(--accent);border-radius:4px}
.hist{font-size:12px;color:var(--t2);columns:2;margin-top:4px}
.sr{position:absolute;left:-9999px}
td.nome{max-width:260px;word-break:break-all}
.abrir{display:flex;gap:12px;align-items:center;margin:0 0 10px}
.abrir .nota{margin:0}
a.btn{display:inline-block;text-decoration:none;color:var(--t1);background:var(--surface);border:1px solid var(--line);
border-radius:7px;padding:6px 12px;font-size:13px;font-weight:600}
a.btn:hover{border-color:var(--accent)}
.card a{color:var(--accent)}
</style></head><body><main class="viz-root">
<h1>Desempenho do extrator de faturas</h1>
<p class="sub" id="sub"></p>

<div class="filtros">
 <label>Distribuidora <select id="fDist"></select></label>
 <label>Mostrar <select id="fSit">
  <option value="">todas as faturas</option>
  <option value="vazio">com campo principal vazio</option>
  <option value="fora">com valor não encontrado no texto</option>
  <option value="consumo">consumo diferente do total medido</option>
  <option value="pend">com campo principal não revisado</option>
  <option value="inc">com campo marcado como incorreto</option>
 </select></label>
 <button id="exp">Exportar revisão (CSV)</button>
 <span class="cont" id="cont"></span>
</div>

<div class="kpis" id="kpis"></div>

<h2>Preenchimento por distribuidora</h2>
<p class="nota">Cada célula: faturas com o campo preenchido / faturas avaliadas. Cód. cliente conta só as faturas que trazem o rótulo do código.</p>
<div class="leg">preenchido: <i class="h0"></i>0–20% <i class="h1"></i>–40% <i class="h2"></i>–60% <i class="h3"></i>–80% <i class="h4"></i>–99% <i class="h5"></i>100%</div>
<div class="scroll"><table class="hm" id="hm"></table></div>

<h2>Como cada fatura foi lida</h2>
<p class="nota">Clique numa fatura para ver o recorte da fatura de onde o modelo tirou cada valor e marcar Correto / Incorreto.</p>
<div class="scroll"><table id="docs"></table></div>

<h2>Campos bônus</h2>
<p class="nota">Faturas em que cada campo veio preenchido. Nas tabelas, também a média de linhas por fatura.</p>
<div class="scroll"><table id="bonus"></table></div>
</main>
<script>
const D = __DADOS__;
const CH = [["uc","UC"],["titular","Titular"],["consumo","Consumo (kWh)"],["cod_cliente","Cód. cliente"]];
const LS = "revisao_faturas::" + D.id;
let rev = {};
try { rev = JSON.parse(localStorage.getItem(LS) || "{}"); } catch(e) {}
D.docs.forEach(d => CH.forEach(([k]) => { const key = d.arquivo+"|"+k; if (!(key in rev) && d.campos[k].revisao) rev[key] = d.campos[k].revisao; }));
const salvar = () => { try { localStorage.setItem(LS, JSON.stringify(rev)); } catch(e) {} };
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const pct = (k,n) => n ? Math.round(100*k/n) + "%" : "–";
const R = (d,k) => rev[d.arquivo+"|"+k] || "";
const esperado = (d,k) => k !== "cod_cliente" || d.campos[k].esperado === true || !!d.campos[k].valor;

function chipTexto(c){
  if (!c.valor) return '<span class="chip vz">vazio</span>';
  if (c.no_texto === true) return '<span class="chip ok">encontrado no texto</span>';
  if (c.no_texto === false) return '<span class="chip no">não encontrado no texto</span>';
  return '<span class="chip na">sem texto OCR</span>';
}
function chipRev(r){ return r==="correto" ? '<span class="chip ok">você: correto</span>' : r==="incorreto" ? '<span class="chip no">você: incorreto</span>' : ""; }

function filtrados(){
  const fd = document.getElementById("fDist").value, fs = document.getElementById("fSit").value;
  return D.docs.filter(d => {
    if (fd && d.distribuidora !== fd) return false;
    const ks = CH.map(x=>x[0]).filter(k => esperado(d,k));
    if (fs==="vazio") return ks.some(k => !d.campos[k].valor);
    if (fs==="fora") return ks.some(k => d.campos[k].no_texto === false);
    if (fs==="consumo") return d.campos.consumo.bate_total === false;
    if (fs==="pend") return ks.some(k => !R(d,k));
    if (fs==="inc") return ks.some(k => R(d,k)==="incorreto");
    return true;
  });
}

function kpis(docs){
  const el = document.getElementById("kpis"); el.innerHTML = "";
  CH.forEach(([k,rot]) => {
    const base = docs.filter(d => esperado(d,k));
    const n = base.length, ext = base.filter(d => d.campos[k].valor);
    const comTxt = ext.filter(d => d.campos[k].no_texto !== null);
    const noTxt = comTxt.filter(d => d.campos[k].no_texto).length;
    const revs = ext.filter(d => R(d,k)), corr = revs.filter(d => R(d,k)==="correto").length;
    let extra = "";
    if (k==="consumo"){
      const cmp = ext.filter(d => d.campos.consumo.bate_total !== null);
      extra = `<li>bate com total medido <b>${cmp.filter(d=>d.campos.consumo.bate_total).length} de ${cmp.length}</b></li>`;
    }
    if (k==="cod_cliente"){
      const semTxt = docs.filter(d => d.campos[k].esperado === null).length;
      extra = `<li>faturas com rótulo do código <b>${docs.filter(d=>d.campos[k].esperado===true).length} de ${docs.length}</b></li>` +
              (semTxt ? `<li>sem texto OCR para verificar <b>${semTxt}</b></li>` : "");
    }
    const p = n ? ext.length/n : 0;
    el.insertAdjacentHTML("beforeend", `<div class="kpi"><div class="l">${rot}</div>
      <div class="v">${n ? pct(ext.length,n) : "–"}</div>
      <div class="s">preenchido em ${ext.length} de ${n} faturas${k==="cod_cliente"?" com rótulo":""}</div>
      <div class="meter" role="img" aria-label="${pct(ext.length,n)} preenchido"><i style="width:${(p*100).toFixed(1)}%"></i></div>
      <ul><li>encontrado no texto da fatura <b>${comTxt.length ? noTxt+" de "+comTxt.length : "–"}</b></li>
      <li>revisado por você <b>${revs.length ? corr+" corretos de "+revs.length : "nenhum"}</b></li>${extra}</ul></div>`);
  });
}

function heat(docs){
  const dists = [...new Set(docs.map(d=>d.distribuidora))].sort();
  const cls = p => p===null ? "" : p>=1 ? "h5" : p>=.8 ? "h4" : p>=.6 ? "h3" : p>=.4 ? "h2" : p>=.2 ? "h1" : "h0";
  let h = "<tr><th>Distribuidora</th><th class='n'>Faturas</th>" + CH.map(([,r])=>`<th style="text-align:center">${r}</th>`).join("") + "</tr>";
  dists.forEach(di => {
    const g = docs.filter(d=>d.distribuidora===di);
    h += `<tr><td>${esc(di)}</td><td class="n">${g.length}</td>`;
    CH.forEach(([k,rot]) => {
      const b = g.filter(d=>esperado(d,k)), e = b.filter(d=>d.campos[k].valor).length;
      const p = b.length ? e/b.length : null;
      h += b.length ? `<td class="c ${cls(p)}" title="${esc(di)} · ${rot}: ${e} de ${b.length} faturas">${e}/${b.length}</td>`
                    : `<td class="c" title="nenhuma fatura com rótulo do código" style="color:var(--t3)">–</td>`;
    });
    h += "</tr>";
  });
  document.getElementById("hm").innerHTML = h;
}

function cardCampo(d,k,rot){
  const c = d.campos[k], r = R(d,k);
  let dl = `<dt>valor lido</dt><dd class="mono">${esc(c.valor) || "∅"}</dd>`;
  if (c.normalizado) dl += `<dt>normalizado</dt><dd class="mono">${esc(c.normalizado)}</dd>`;
  if (k==="consumo"){
    dl += `<dt>mês de referência</dt><dd>${esc(c.mes)||"–"} <span style="color:var(--t3)">(mais recente de ${c.n_meses} meses)</span></dd>`;
    dl += `<dt>total medido</dt><dd class="mono">${esc(c.total_medido)||"∅"} ${c.bate_total===true?'<span class="chip ok">igual</span>':c.bate_total===false?'<span class="chip no">diferente</span>':""}</dd>`;
  }
  if (k==="cod_cliente") dl += `<dt>rótulo na fatura</dt><dd>${c.esperado===null?"sem texto OCR":c.rotulo?esc(c.rotulo):"não encontrado"}</dd>`;
  dl += `<dt>confiança</dt><dd>${c.confianca ?? "–"}</dd><dt>página / posição</dt><dd>${esc(c.pagina)||"–"} ${esc(c.posicao)}${c.pagina && d.existe ? ` · <a href="${linkFatura(d,c.pagina)}" target="_blank" rel="noopener">abrir nesta página</a>` : ""}</dd>`;
  if (c.ocorrencias.length > 1) dl += `<dt>ocorrências</dt><dd class="mono">${c.ocorrencias.map(esc).join("<br>")}</dd>`;
  dl += `<dt>no texto</dt><dd>${chipTexto(c)}</dd>`;
  const img = c.recorte ? `<img src="${esc(c.recorte)}" alt="Recorte da fatura onde o modelo leu ${rot}" loading="lazy">`
           : `<p class="nota">${c.valor ? "sem posição para recortar" : "o modelo não retornou este campo"}</p>`;
  const hist = (k==="consumo" && c.historico.length) ? `<details><summary class="nota">histórico extraído (${c.historico.length})</summary><div class="hist mono">${c.historico.map(([m,v])=>esc(m)+": "+(esc(v)||"∅")).join("<br>")}</div></details>` : "";
  return `<div class="card"><h3>${rot}</h3>${img}<dl>${dl}</dl>${hist}
    <div class="rev" data-a="${esc(d.arquivo)}" data-k="${k}">
      <button data-v="correto" class="${r==="correto"?"sel-ok":""}" aria-pressed="${r==="correto"}">✓ Correto</button>
      <button data-v="incorreto" class="${r==="incorreto"?"sel-no":""}" aria-pressed="${r==="incorreto"}">✗ Incorreto</button>
      <button data-v="">limpar</button></div></div>`;
}

function linkFatura(d, pagina){
  const pdf = /\.pdf$/i.test(d.arquivo);
  return d.link + (pdf && pagina ? "#page=" + encodeURIComponent(pagina) : "");
}
function barraFatura(d){
  if (!d.existe) return `<p class="nota">Arquivo da fatura não encontrado em documentos/ quando o dashboard foi gerado.</p>`;
  return `<div class="abrir"><a class="btn" href="${linkFatura(d)}" target="_blank" rel="noopener">Abrir fatura ↗</a>
    <span class="nota mono">${esc(d.arquivo)}</span></div>`;
}

let aberto = null;
function tabela(docs){
  let h = "<tr><th>Fatura</th><th>Distribuidora</th>" + CH.map(([,r])=>`<th>${r}</th>`).join("") + "</tr>";
  docs.forEach(d => {
    h += `<tr class="doc" data-a="${esc(d.arquivo)}" tabindex="0"><td class="mono nome" title="${esc(d.arquivo)}">${esc(d.arquivo)}</td><td>${esc(d.distribuidora)}</td>`;
    CH.forEach(([k]) => {
      const c = d.campos[k];
      if (!esperado(d,k)) { h += `<td><span class="chip na">não se aplica</span></td>`; return; }
      const v = k==="consumo" && c.valor ? `${esc(c.valor)} <span style="color:var(--t3)">${esc(c.mes)}</span>` : esc(c.valor) || "∅";
      h += `<td><div class="mono">${v}</div>${chipTexto(c)} ${chipRev(R(d,k))}</td>`;
    });
    h += "</tr>";
    if (aberto === d.arquivo) h += `<tr class="det"><td colspan="${2+CH.length}">${barraFatura(d)}<div class="cards">${CH.map(([k,r])=>cardCampo(d,k,r)).join("")}</div></td></tr>`;
  });
  if (!docs.length) h += `<tr><td colspan="${2+CH.length}" class="nota">Nenhuma fatura neste filtro.</td></tr>`;
  document.getElementById("docs").innerHTML = h;
}

function bonus(docs){
  const n = docs.length;
  let h = "<tr><th>Campo</th><th class='n'>Faturas com valor</th><th class='n'>%</th><th></th><th class='n'>Média de linhas</th></tr>";
  D.campos_bonus.forEach(([campo, tabelaPai]) => {
    const com = docs.filter(d => (d.preenchidos[campo]||0) > 0).length;
    const media = tabelaPai===null && D.tabelas.includes(campo) ? (docs.reduce((s,d)=>s+(d.preenchidos[campo]||0),0)/(n||1)).toFixed(1) : "";
    const nome = campo.includes(".") ? "&nbsp;&nbsp;&nbsp;↳ " + esc(campo.split(".")[1]) : esc(campo);
    h += `<tr><td class="mono">${nome}</td><td class="n">${com} de ${n}</td><td class="n">${pct(com,n)}</td>
      <td><div class="bar" role="img" aria-label="${pct(com,n)}"><i style="width:${n?100*com/n:0}%"></i></div></td><td class="n">${media}</td></tr>`;
  });
  document.getElementById("bonus").innerHTML = h;
}

function render(){
  const docs = filtrados();
  document.getElementById("cont").textContent = `${docs.length} de ${D.docs.length} faturas`;
  kpis(docs); heat(docs); tabela(docs); bonus(docs);
}

document.getElementById("sub").textContent = `${D.docs.length} faturas · ${D.processador} · gerado em ${D.gerado}`;
const sel = document.getElementById("fDist");
sel.innerHTML = '<option value="">todas</option>' + [...new Set(D.docs.map(d=>d.distribuidora))].sort().map(x=>`<option>${esc(x)}</option>`).join("");
["fDist","fSit"].forEach(id => document.getElementById(id).addEventListener("change", render));
document.getElementById("docs").addEventListener("click", e => {
  const b = e.target.closest(".rev button");
  if (b) { const p = b.parentElement, key = p.dataset.a+"|"+p.dataset.k;
           if (b.dataset.v) rev[key] = b.dataset.v; else delete rev[key]; salvar(); render(); return; }
  if (e.target.closest("a")) return;
  const tr = e.target.closest("tr.doc"); if (!tr) return;
  aberto = aberto === tr.dataset.a ? null : tr.dataset.a; render();
});
document.getElementById("docs").addEventListener("keydown", e => {
  const tr = e.target.closest("tr.doc"); if (tr && (e.key==="Enter"||e.key===" ")) { e.preventDefault(); tr.click(); }
});
document.getElementById("exp").addEventListener("click", () => {
  const lin = [["arquivo","distribuidora","campo","valor_extraido","avaliacao"]];
  D.docs.forEach(d => CH.forEach(([k]) => { const r = R(d,k); if (r) lin.push([d.arquivo,d.distribuidora,k,d.campos[k].valor,r]); }));
  const csv = "﻿" + lin.map(l => l.map(v => '"' + String(v ?? "").replace(/"/g,'""') + '"').join(";")).join("\r\n");
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([csv], {type:"text/csv;charset=utf-8"}));
  a.download = "revisao_faturas.csv"; a.click();
});
render();
</script></body></html>"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sem-recortes", action="store_true")
    a = ap.parse_args()

    if not os.path.exists(M["cache"]):
        raise SystemExit(f"{M['cache']} não encontrado: rode antes `python extrair.py`.")
    with open(M["cache"], encoding="utf-8") as f:
        respostas = json.load(f)
    respostas = {k: v for k, v in respostas.items() if "entidades" in v}
    if not respostas:
        raise SystemExit("Nenhuma fatura processada com sucesso no cache.")

    revisoes = carregar_revisoes()
    rec = Recortador(M["pasta"], SAIDA, ativo=not a.sem_recortes)
    docs = []
    for i, (arquivo, resp) in enumerate(sorted(respostas.items()), 1):
        print(f"\r[{i}/{len(respostas)}] {arquivo[:40]:<40}", end="", flush=True)
        docs.append(montar_doc(arquivo, resp, rec, revisoes))
    print()

    campos_bonus = []
    tabelas = []
    for campo, filhos in M["schema"].items():
        if campo in ("installation_id", "customer_name", "account_contract_id"):
            continue
        campos_bonus.append([campo, None])
        if filhos:
            tabelas.append(campo)
            campos_bonus += [[f"{campo}.{f}", campo] for f in filhos]

    procs = sorted({r.get("processador", "") for r in respostas.values()} - {""})
    dados = {
        "id": _norm(os.path.abspath(M["cache"]))[-40:],
        "processador": procs[0].split("/processors/")[-1] if len(procs) == 1 else f"{len(procs)} processadores/versões",
        "gerado": datetime.now().strftime("%d/%m/%Y %H:%M"),
        "docs": docs, "campos_bonus": campos_bonus, "tabelas": tabelas,
    }
    js = json.dumps(dados, ensure_ascii=False).replace("</", "<\\/")
    with open(os.path.join(SAIDA, "index.html"), "w", encoding="utf-8") as f:
        f.write(HTML.replace("__DADOS__", js))

    # resumo no terminal
    n = len(docs)
    print(f"{n} faturas")
    for cid, rot, _ in CHAVE:
        base = [d for d in docs if cid != "cod_cliente" or d["campos"][cid].get("esperado") or d["campos"][cid]["valor"]]
        k = sum(1 for d in base if d["campos"][cid]["valor"])
        print(f"  {rot:<15} preenchido {k}/{len(base)}")
    print(f"Abra {SAIDA}/index.html no navegador.")


if __name__ == "__main__":
    main()