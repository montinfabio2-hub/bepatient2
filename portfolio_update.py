"""Aggiorna data.json (file PUBBLICO) a partire dal registro acquisti PRIVATO.

Il registro acquisti non e' nella repository: arriva dal Secret di GitHub
Actions PORTFOLIO_LEDGER (formato e istruzioni in docs/registro-acquisti.md).
Quantita', prezzi di acquisto, prezzi medi, capitale investito e valori
assoluti restano nella memoria dello script e NON vengono mai scritti in
data.json ne' stampati nei log: nel file pubblico finiscono solo percentuali.

Gli errori sul registro sono segnalati con numero di riga e nome del campo,
mai con il contenuto.
"""
import json
import os
import re
import sys
import tempfile
import urllib.request
from collections import defaultdict
from datetime import date, datetime

ETFS = {
    'UST':  'UST.PA',
    'EUNL': 'EUNL.DE',
    'VWCE': 'VWCE.DE',
    'IS3R': 'IS3R.DE',
}
PORTFOLIOS = ('personal', 'family')
LEDGER_ENV = 'PORTFOLIO_LEDGER'
MAX_LEDGER_CHARS = 200_000
MAX_LEDGER_ROWS = 5_000


class LedgerError(Exception):
    """Registro acquisti mancante o non valido. Il messaggio non contiene mai dati del registro."""


class PriceError(Exception):
    """Prezzo di mercato non disponibile e nessun ultimo prezzo noto. Il messaggio non contiene dati del registro."""


class SchemaError(Exception):
    """Il JSON pubblico non rispetta lo schema consentito. Il messaggio non contiene valori."""


# ── REGISTRO ACQUISTI (privato) ──────────────────────────────────────────────

_RE_DATE = re.compile(r'\d{4}-\d{2}-\d{2}')
_RE_QTY = re.compile(r'[1-9]\d{0,6}')
_RE_PRICE = re.compile(r'\d{1,6}(\.\d{1,6})?')


def parse_ledger(text, today=None):
    """Legge il registro: una riga per acquisto 'portafoglio,data,ticker,quantita,prezzo'.

    Ritorna {portafoglio: [(data_iso, ticker, quantita:int, prezzo:float), ...]}.
    Righe vuote e commenti ('#' fino a fine riga) sono ignorati.
    """
    today = today or date.today()
    if text is None or not text.strip():
        raise LedgerError(f'Secret {LEDGER_ENV} mancante o vuoto')
    if len(text) > MAX_LEDGER_CHARS:
        raise LedgerError('registro troppo grande')
    if '\x00' in text:
        raise LedgerError('registro non valido (caratteri non ammessi)')

    ledger = {name: [] for name in PORTFOLIOS}
    rows = 0
    for n, raw in enumerate(text.lstrip('﻿').splitlines(), 1):
        line = raw.split('#', 1)[0].strip()
        if not line:
            continue
        rows += 1
        if rows > MAX_LEDGER_ROWS:
            raise LedgerError('troppe righe nel registro')
        f = [x.strip() for x in line.split(',')]
        if len(f) != 5:
            raise LedgerError(f'riga {n}: attesi 5 campi separati da virgola '
                              '(portafoglio,data,ticker,quantita,prezzo)')
        pf, d, ticker, qty, price = f
        if pf not in PORTFOLIOS:
            raise LedgerError(f'riga {n}: portafoglio non riconosciuto (ammessi: {", ".join(PORTFOLIOS)})')
        if not _RE_DATE.fullmatch(d):
            raise LedgerError(f'riga {n}: data non valida (formato AAAA-MM-GG)')
        try:
            dd = datetime.strptime(d, '%Y-%m-%d').date()
        except ValueError:
            raise LedgerError(f'riga {n}: data non valida (formato AAAA-MM-GG)') from None
        if dd.year < 2000 or dd > today:
            raise LedgerError(f'riga {n}: data fuori intervallo (non nel futuro, dal 2000)')
        if ticker not in ETFS:
            raise LedgerError(f'riga {n}: ticker non riconosciuto (ammessi: {", ".join(ETFS)})')
        if not _RE_QTY.fullmatch(qty):
            raise LedgerError(f'riga {n}: quantita non valida (intero positivo)')
        if not _RE_PRICE.fullmatch(price) or float(price) <= 0:
            raise LedgerError(f'riga {n}: prezzo non valido (numero positivo con il punto decimale)')
        ledger[pf].append((d, ticker, int(qty), float(price)))

    for name in PORTFOLIOS:
        if not ledger[name]:
            raise LedgerError(f'nessun acquisto per il portafoglio "{name}"')
    return ledger


# ── PREZZI (invariato rispetto alla versione precedente) ─────────────────────

def fetch_price(symbol):
    url = f"https://query2.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=5d"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
        result = data['chart']['result'][0]
        price = result['meta']['regularMarketPrice']
        return round(price, 4)
    except Exception as e:
        print(f"Error fetching {symbol}: {e}")
        return None


def fetch_prices():
    print("Fetching current prices...")
    prices = {}
    for ticker, symbol in ETFS.items():
        p = fetch_price(symbol)
        if p:
            prices[ticker] = p
            print(f"  {ticker}: {p}")
    return prices


# ── PREZZI MANCANTI: si usa l'ultimo prezzo noto e si segnala ────────────────
# Gli ultimi prezzi di mercato (pubblici, non fanno parte del registro) stanno in
# data.json > portfolio > last_prices come {"TICKER": {"close": 123.45, "date": "AAAA-MM-GG"}}.

def load_last_prices(path):
    """Ultimi prezzi noti da data.json; voci non valide o file assente/illeggibile: ignorati."""
    try:
        with open(path, 'r') as f:
            raw = json.load(f).get('portfolio', {}).get('last_prices', {})
    except (OSError, ValueError, AttributeError):
        return {}
    valid = {}
    if isinstance(raw, dict):
        for t, v in raw.items():
            if t in ETFS and _valid_last_price(v):
                valid[t] = {'close': v['close'], 'date': v['date']}
    return valid


def resolve_prices(live, last, needed, today):
    """Prezzi da usare nei calcoli.

    Ritorna (prezzi, ultimi_prezzi_da_salvare, ticker_non_aggiornati).
    Se manca il prezzo di un ETF presente nel registro si usa l'ultimo noto; se non
    esiste nemmeno quello si solleva PriceError (nessun file viene modificato).
    """
    saved = {t: dict(v) for t, v in last.items()}
    prices, stale, missing = {}, [], []
    for t, p in live.items():
        if _is_num(p) and p > 0:
            saved[t] = {'close': p, 'date': str(today)}
    for t in sorted(needed):
        p = live.get(t)
        if _is_num(p) and p > 0:
            prices[t] = p
        elif t in last:
            prices[t] = last[t]['close']
            stale.append(t)
        else:
            missing.append(t)
    if missing:
        raise PriceError('prezzo non disponibile e nessun ultimo prezzo salvato per: ' + ', '.join(missing))
    return prices, saved, stale


def _signal_stale(tickers):
    """Fa risultare 'fallito' il workflow (ultimo passaggio) cosi' GitHub invia l'avviso."""
    path = os.environ.get('GITHUB_OUTPUT')
    if path:
        with open(path, 'a') as f:
            f.write('stale=1\n')
    print('::warning::Prezzo non disponibile per ' + ', '.join(tickers) + ': usato l\'ultimo prezzo noto')


# ── CALCOLI (logica invariata; i dati riservati restano interni) ─────────────

def calc_holdings(purchases, prices):
    """Calcola qty totale, costo medio, valore attuale e % per ogni ETF (uso interno)."""
    h = defaultdict(lambda: {'qty': 0, 'cost': 0.0})
    for _, t, q, p in purchases:
        h[t]['qty'] += q
        h[t]['cost'] += q * p
    result = {}
    for ticker, v in h.items():
        if v['qty'] == 0:
            continue
        avg = v['cost'] / v['qty']
        cur = prices.get(ticker, avg)  # NB: ripiego da correggere nella Fase B
        perf = (cur - avg) / avg * 100
        result[ticker] = {
            'qty': v['qty'],
            'avgPrice': round(avg, 4),
            'currentPrice': cur,
            'perf_pct': round(perf, 2),
            'invested': round(v['cost'], 2),
            'value': round(v['qty'] * cur, 2),
        }
    return result


def portfolio_perf(holdings):
    total_invested = sum(h['invested'] for h in holdings.values())
    total_value = sum(h['value'] for h in holdings.values())
    if total_invested == 0:
        return 0.0
    return round((total_value - total_invested) / total_invested * 100, 2)


def build_monthly(purchases, prices, today, end_month=None):
    if end_month is None:
        end_month = today.strftime('%Y-%m')
    purchases = sorted(purchases, key=lambda x: x[0])
    start_d = datetime.strptime(purchases[0][0][:7], '%Y-%m').date()
    end_d = datetime.strptime(end_month, '%Y-%m').date()
    months = []
    d = start_d
    while d <= end_d:
        months.append(d.strftime('%Y-%m'))
        d = date(d.year + 1, 1, 1) if d.month == 12 else date(d.year, d.month + 1, 1)

    def get_price(ticker, month_str):
        pts = [(datetime.strptime(p[0], '%Y-%m-%d').date().toordinal(), p[3])
               for p in purchases if p[1] == ticker]
        pts.append((today.toordinal(), prices.get(ticker, pts[-1][1] if pts else 0)))
        pts.sort()
        target = datetime.strptime(month_str + '-15', '%Y-%m-%d').date().toordinal()
        if target <= pts[0][0]:
            return pts[0][1]
        if target >= pts[-1][0]:
            return pts[-1][1]
        for i in range(len(pts) - 1):
            x0, y0 = pts[i]
            x1, y1 = pts[i + 1]
            if x0 <= target <= x1:
                return y0 + (target - x0) / (x1 - x0) * (y1 - y0)
        return pts[-1][1]

    h = defaultdict(lambda: {'qty': 0, 'cost': 0.0})
    pi = 0
    result = []
    for month in months:
        while pi < len(purchases) and purchases[pi][0][:7] == month:
            _, t, q, p = purchases[pi]
            h[t]['qty'] += q
            h[t]['cost'] += q * p
            pi += 1
        total_cost = sum(v['cost'] for v in h.values())
        if total_cost == 0:
            continue
        total_value = sum(v['qty'] * get_price(t, month) for t, v in h.items() if v['qty'] > 0)
        perf = round((total_value - total_cost) / total_cost * 100, 2)
        result.append({'m': month, 'pct': perf})
    return result


def build_public_section(purchases, prices, today):
    """Sezione pubblica di un portafoglio: SOLO percentuali e pesi percentuali."""
    holdings = calc_holdings(purchases, prices)
    perf = portfolio_perf(holdings)
    history = build_monthly(purchases, prices, today)
    # l'ultimo punto coincide con la performance corrente
    if history and history[-1]['m'] == today.strftime('%Y-%m'):
        history[-1]['pct'] = perf
    total_value = sum(h['value'] for h in holdings.values())
    public_holdings = {
        t: {
            'perf_pct': h['perf_pct'],
            'weight_pct': round(h['value'] / total_value * 100, 2) if total_value > 0 else 0.0,
        }
        for t, h in holdings.items()
    }
    return {'performance_pct': perf, 'holdings': public_holdings, 'history': history}


def build_public_portfolio(ledger, prices, today):
    out = {name: build_public_section(ledger[name], prices, today) for name in PORTFOLIOS}
    out['last_update'] = str(today)
    return out


# ── SCHEMA DEL JSON PUBBLICO (elenco di campi ammessi, non ricerca di parole) ─

_RE_MONTH = re.compile(r'\d{4}-(0[1-9]|1[0-2])')


def _is_num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and x == x and abs(x) != float('inf')


def _valid_last_price(v):
    return (isinstance(v, dict) and set(v) == {'close', 'date'}
            and _is_num(v['close']) and 0 < v['close'] < 1_000_000
            and isinstance(v['date'], str) and bool(_RE_DATE.fullmatch(v['date'])))


def validate_public_portfolio(p):
    """Verifica che 'portfolio' contenga solo i campi pubblici consentiti."""
    base = {*PORTFOLIOS, 'last_update'}
    if not isinstance(p, dict) or set(p) not in (base, base | {'last_prices'}):
        raise SchemaError('portfolio: chiavi non ammesse o mancanti')
    if 'last_prices' in p:
        lp = p['last_prices']
        if not isinstance(lp, dict) or not all(t in ETFS and _valid_last_price(v) for t, v in lp.items()):
            raise SchemaError('portfolio.last_prices non valido')
    if not isinstance(p['last_update'], str) or not _RE_DATE.fullmatch(p['last_update']):
        raise SchemaError('portfolio.last_update non valido')
    for name in PORTFOLIOS:
        s = p[name]
        if not isinstance(s, dict) or set(s) != {'performance_pct', 'holdings', 'history'}:
            raise SchemaError(f'portfolio.{name}: chiavi non ammesse o mancanti')
        if not _is_num(s['performance_pct']) or not -100 <= s['performance_pct'] <= 100000:
            raise SchemaError(f'portfolio.{name}.performance_pct non valido')
        if not isinstance(s['holdings'], dict) or not s['holdings']:
            raise SchemaError(f'portfolio.{name}.holdings non valido')
        for t, h in s['holdings'].items():
            if t not in ETFS or not isinstance(h, dict) or set(h) != {'perf_pct', 'weight_pct'}:
                raise SchemaError(f'portfolio.{name}.holdings: voce non ammessa')
            if not _is_num(h['perf_pct']) or not _is_num(h['weight_pct']) or not 0 <= h['weight_pct'] <= 100:
                raise SchemaError(f'portfolio.{name}.holdings: valori non validi')
        if not isinstance(s['history'], list) or len(s['history']) < 1:
            raise SchemaError(f'portfolio.{name}.history non valida')
        for pt in s['history']:
            if (not isinstance(pt, dict) or set(pt) != {'m', 'pct'}
                    or not isinstance(pt['m'], str) or not _RE_MONTH.fullmatch(pt['m'])
                    or not _is_num(pt['pct'])):
                raise SchemaError(f'portfolio.{name}.history: punto non valido')


# ── SCRITTURA ────────────────────────────────────────────────────────────────

def update_data_file(path, ledger, prices, today, last_prices=None):
    """Calcola e scrive data.json solo se tutto e' valido (scrittura atomica).

    Gli altri campi del file (km, ecc.) restano invariati; il blocco 'portfolio'
    viene sostituito per intero, quindi eventuali campi riservati di versioni
    precedenti scompaiono.
    """
    try:
        with open(path, 'r') as f:
            site_data = json.load(f)
    except FileNotFoundError:
        site_data = {}

    portfolio = build_public_portfolio(ledger, prices, today)
    if last_prices is not None:
        portfolio['last_prices'] = last_prices
    validate_public_portfolio(portfolio)
    site_data['portfolio'] = portfolio

    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=directory, prefix='.data-', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(site_data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return portfolio


def main(data_path='data.json'):
    step = 'lettura del registro'
    try:
        today = date.today()
        ledger = parse_ledger(os.environ.get(LEDGER_ENV), today)
        step = 'recupero dei prezzi'
        live = fetch_prices()
        last = load_last_prices(data_path)
        needed = {t for rows in ledger.values() for _, t, _, _ in rows}
        prices, saved, stale = resolve_prices(live, last, needed, today)
        step = 'calcolo e scrittura di data.json'
        pub = update_data_file(data_path, ledger, prices, today, last_prices=saved)
    except (LedgerError, SchemaError, PriceError) as e:
        print(f'ERRORE ({step}): {e}', file=sys.stderr)
        return 1
    except Exception as e:  # nessun traceback: potrebbe riportare dati del registro
        print(f'ERRORE inatteso durante {step} ({type(e).__name__}). '
              'Nessun file e\' stato modificato.', file=sys.stderr)
        return 1
    print(f"Personal: {pub['personal']['performance_pct']}%  Family: {pub['family']['performance_pct']}%")
    print("data.json updated")
    if stale:
        for t in stale:
            print(f"ATTENZIONE: prezzo di {t} non disponibile, usato l'ultimo noto del {last[t]['date']}")
        _signal_stale(stale)
    return 0


if __name__ == '__main__':
    sys.exit(main())
