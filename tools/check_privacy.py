#!/usr/bin/env python3
"""Controllo anti-esposizione dei dati riservati del portafoglio.

Verifica che nei file della repository (che potrebbero essere distribuiti al
browser) non compaiano quantita', prezzi di acquisto, prezzi medi, capitale
investito o il registro acquisti. NON e' una semplice ricerca di parole:

 Livello 1 (sempre attivo, non richiede il registro)
   - JSON: per data.json il blocco 'portfolio' deve rispettare lo schema con
     l'elenco dei soli campi pubblici; in qualunque JSON sono vietate chiavi
     tipo quantita'/prezzo medio/valore/acquisti (confronto sui NOMI DELLE CHIAVI).
   - Testo: righe nel formato del registro (portafoglio,data,ticker,qta,prezzo)
     e record di acquisto (ticker + numero intero + numero decimale sulla
     stessa riga, fuori dalle stringhe).

 Livello 2 (se il registro e' disponibile: variabile PORTFOLIO_LEDGER o --ledger-file)
   - impronte dei VALORI reali: coppia quantita'+prezzo di una riga del registro
     vicine nel testo, quantita' totale per ETF accanto al ticker, capitale
     investito, prezzo medio accanto al ticker. Richiedono coincidenze
     multiple, per evitare falsi positivi su numeri qualsiasi.

Il rapporto indica file, riga e tipo di problema: MAI i valori trovati.
Esito: 0 = nessun problema, 1 = esposizioni trovate, 2 = uso o registro non validi.
Le righe con il marcatore ESEMPIO-FITTIZIO sono ignorate dal livello 1
(servono per gli esempi nella documentazione); il livello 2 non le ignora mai.
"""
import argparse
import json
import os
import re
import subprocess
import sys
from datetime import date

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import portfolio_update as pu  # noqa: E402

EXEMPT = 'ESEMPIO-FITTIZIO'
TICKER_RE = re.compile(r'\b(' + '|'.join(map(re.escape, pu.ETFS)) + r')\b')
# numeri "veri" nel codice: non dentro stringhe/URL/identificatori/unita' (11px, 0.12em, /251882/)
NUM_STRICT = re.compile(r"(?<![\w./'\"-])(\d+(?:\.\d+)?)(?![\w./'\"-])")
# per il livello 2: anche dentro stringhe, ma non dentro parole o numeri piu' lunghi
NUM_LOOSE = re.compile(r'(?<![\w.-])(\d+(?:\.\d+)?)(?![\w-]|\.\d)')   # esclude parti di date (2026-01-05)
LEDGER_ROW_RE = re.compile(
    r'^\s*(?:personal|family)\s*,\s*\d{4}-\d{2}-\d{2}\s*,\s*(?:' + '|'.join(pu.ETFS) + r')\s*,\s*\d+\s*,\s*\d+(?:\.\d+)?',
    re.M)
FORBIDDEN_KEYS = {'qty', 'quantity', 'quantita', 'avgprice', 'avg_price', 'invested', 'value',
                  'prices', 'currentprice', 'purchases', 'ledger', 'cost'}
ROW_WINDOW = 120      # caratteri entro cui quantita' e prezzo contano come "stesso record"
TICKER_WINDOW = 200     # prezzo medio: il ticker puo' essere nella riga/oggetto vicino
QTY_TICKER_WINDOW = 40  # quantita' totale: deve stare praticamente accanto al ticker


def _line_of(text, pos):
    return text.count('\n', 0, pos) + 1


def _strip_svg_geometry(text):
    """I grafici SVG hanno migliaia di coordinate decimali: non sono dati del registro."""
    return re.sub(r'(points|d)="[^"]*"', lambda m: ' ' * len(m.group(0)), text)


def _decimals(tok):
    return len(tok.split('.')[1]) if '.' in tok else 0


# ── Livello 1 ────────────────────────────────────────────────────────────────

def scan_json(path, text):
    findings = []
    try:
        data = json.loads(text)
    except ValueError:
        return [(path, 1, 'JSON-non-valido', 'file JSON non leggibile')]

    def walk(node, trail):
        if isinstance(node, dict):
            for k, v in node.items():
                if k.lower() in FORBIDDEN_KEYS:
                    findings.append((path, 1, 'L1-chiave-vietata',
                                     f'chiave non ammessa in {trail or "radice"}'))
                walk(v, f'{trail}.{k}' if trail else k)
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f'{trail}[{i}]')
    walk(data, '')
    if os.path.basename(path) == 'data.json' and isinstance(data, dict) and 'portfolio' in data:
        try:
            pu.validate_public_portfolio(data['portfolio'])
        except pu.SchemaError as e:
            findings.append((path, 1, 'L1-schema', str(e)))
    return findings


def scan_text_level1(path, text):
    findings = []
    lines = text.splitlines()
    for m in LEDGER_ROW_RE.finditer(text):
        ln = _line_of(text, m.start())
        if EXEMPT not in lines[ln - 1]:
            findings.append((path, ln, 'L1-riga-registro', 'riga nel formato del registro acquisti'))
    # Il controllo "ticker + intero + decimale" e' euristico: non si applica a prosa e
    # documentazione (.md) ne' ai test, dove i dati fittizi sono voluti. Il livello 2
    # (valori reali del registro) li controlla comunque.
    if path.replace(os.sep, '/').startswith('tests/') or path.lower().endswith('.md'):
        return findings
    for i, line in enumerate(lines, 1):
        if EXEMPT in line or not TICKER_RE.search(line):
            continue
        nums = NUM_STRICT.findall(line)
        if any('.' not in n for n in nums) and any('.' in n for n in nums):
            findings.append((path, i, 'L1-record-acquisto',
                             'ticker con numero intero e decimale (possibile quantita e prezzo)'))
    return findings


# ── Livello 2 ────────────────────────────────────────────────────────────────

def ledger_fingerprints(ledger):
    """Impronte derivate dal registro (restano in memoria, non vengono mai stampate)."""
    rows, totals = [], []
    grand = {}
    for name, purchases in ledger.items():
        per = {}
        for _, t, q, p in purchases:
            d = per.setdefault(t, [0, 0.0])
            d[0] += q
            d[1] += q * p
            rows.append((t, q, p))
        for t, (q, cost) in per.items():
            totals.append((t, q, round(cost, 2), round(cost / q, 4)))
        grand[name] = round(sum(c for _, c in per.values()), 2)
    return {'rows': rows, 'totals': totals, 'grand': list(grand.values())}


def scan_text_level2(path, text, fp):
    findings = []
    clean = _strip_svg_geometry(text)
    toks = [(m.start(), m.group(1)) for m in NUM_LOOSE.finditer(clean)]
    ints = [(pos, int(t)) for pos, t in toks if '.' not in t]
    decs = [(pos, t) for pos, t in toks if '.' in t]
    tickers = [m.start() for m in TICKER_RE.finditer(clean)]

    def near_ticker(pos, window=TICKER_WINDOW):
        return any(abs(pos - tp) <= window for tp in tickers)

    seen = set()

    def add(pos, code, msg):
        key = (_line_of(text, pos), code)
        if key not in seen:
            seen.add(key)
            findings.append((path, key[0], code, msg))

    # a) riga del registro: quantita' e prezzo vicini
    for _t, q, price in fp['rows']:
        q_pos = [pos for pos, v in ints if v == q]
        if not q_pos:
            continue
        for pos, tok in decs:
            if float(tok) == price and _decimals(tok) >= min(2, _decimals(repr(price))):
                if any(abs(pos - qp) <= ROW_WINDOW for qp in q_pos):
                    add(pos, 'L2-riga-registro', 'quantita e prezzo di un acquisto del registro')
    # b) totali per ETF
    for t, q_tot, invested, avg in fp['totals']:
        for pos, v in ints:
            if v == q_tot and near_ticker(pos, QTY_TICKER_WINDOW):
                add(pos, 'L2-quantita-totale', 'quantita totale detenuta accanto a un ticker')
        for pos, tok in decs:
            x = float(tok)
            if abs(x - invested) < 0.0051 and invested >= 1000:
                add(pos, 'L2-capitale', 'capitale investito di un ETF')
            if (_decimals(tok) >= 2 or x == avg) and (abs(x - avg) < 0.0051 or x == avg) and near_ticker(pos):
                add(pos, 'L2-prezzo-medio', 'prezzo medio di acquisto accanto a un ticker')
    # c) capitale totale per portafoglio
    for grand in fp['grand']:
        for pos, tok in decs:
            if abs(float(tok) - grand) < 0.0051 and grand >= 1000:
                add(pos, 'L2-capitale', 'capitale investito totale')
    return findings


# ── Scansione ────────────────────────────────────────────────────────────────

def list_files(root):
    try:
        out = subprocess.run(['git', '-C', root, 'ls-files', '-co', '--exclude-standard'],
                             capture_output=True, text=True, check=True).stdout.split('\n')
        files = [f for f in out if f]
    except (OSError, subprocess.CalledProcessError):
        files = []
        for base, dirs, names in os.walk(root):
            dirs[:] = [d for d in dirs if d != '.git']
            files += [os.path.relpath(os.path.join(base, n), root) for n in names]
    return sorted(set(files))


def scan_root(root, ledger=None):
    findings, scanned = [], 0
    fp = ledger_fingerprints(ledger) if ledger else None
    for rel in list_files(root):
        full = os.path.join(root, rel)
        if not os.path.isfile(full):
            continue
        try:
            with open(full, encoding='utf-8') as f:
                text = f.read()
        except (UnicodeDecodeError, OSError):
            continue  # file binari (immagini): nessun testo da analizzare
        scanned += 1
        if rel.lower().endswith('.json'):
            findings += scan_json(rel, text)
        else:
            findings += scan_text_level1(rel, text)
        if fp:
            if rel.lower().endswith('.json'):
                # nei JSON valgono schema e chiavi: qui solo l'impronta quantita'+prezzo
                findings += [f for f in scan_text_level2(rel, text, fp) if f[2] == 'L2-riga-registro']
            else:
                findings += scan_text_level2(rel, text, fp)
    return findings, scanned


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--root', default=ROOT)
    ap.add_argument('--ledger-file', help='file con il registro (altrimenti variabile %s)' % pu.LEDGER_ENV)
    args = ap.parse_args(argv)

    ledger_text = None
    if args.ledger_file:
        try:
            with open(args.ledger_file, encoding='utf-8') as f:
                ledger_text = f.read()
        except OSError:
            print('ERRORE: impossibile leggere il file del registro', file=sys.stderr)
            return 2
    else:
        ledger_text = os.environ.get(pu.LEDGER_ENV) or None

    ledger = None
    if ledger_text:
        try:
            ledger = pu.parse_ledger(ledger_text, date.today())
        except pu.LedgerError as e:
            print(f'ERRORE registro: {e}', file=sys.stderr)
            return 2

    findings, scanned = scan_root(args.root, ledger)
    level = 'livelli 1+2' if ledger else 'solo livello 1 (registro non disponibile)'
    if findings:
        print(f'ESPOSIZIONI TROVATE: {len(findings)} ({level}, {scanned} file analizzati)')
        for path, line, code, msg in sorted(findings):
            print(f'  {path}:{line}  [{code}] {msg}')
        return 1
    print(f'Nessuna esposizione trovata ({level}, {scanned} file analizzati)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
