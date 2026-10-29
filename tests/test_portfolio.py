"""Test di portfolio_update.py con dati FITTIZI (nessun dato reale, nessuna rete).

Esecuzione:  python3 -m unittest discover -s tests -v
I numeri dei registri di prova sono scritti come stringhe: cosi' i file di test
non assomigliano a un registro vero e non fanno scattare tools/check_privacy.py.
"""
import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import portfolio_update as pu  # noqa: E402
from datetime import date  # noqa: E402

TODAY = date(2026, 10, 17)
SCRIPT = os.path.join(ROOT, 'portfolio_update.py')
CANARY = 'CANARY'          # testo che non deve MAI comparire in log, errori o file
CANARY_QTY = '7777771'
CANARY_PRICE = '3131.4159'


def row(pf, d, t, q, p):
    return ','.join([pf, d, t, q, p])


def ledger_text(*rows):
    return '\n'.join(rows) + '\n'


def base_rows():
    return [
        row('personal', '2026-01-05', 'UST', '10', '100.00'),
        row('personal', '2026-02-05', 'UST', '10', '120.00'),
        row('personal', '2026-03-05', 'EUNL', '100', '50.00'),
        row('family', '2026-04-01', 'EUNL', '20', '100.00'),
    ]


BASE_PRICES = {'UST': 130.0, 'EUNL': 40.0}


def load_json(path):
    with open(path) as f:
        return json.load(f)


def read_text(path):
    with open(path) as f:
        return f.read()


def sha(path):
    with open(path, 'rb') as f:
        return hashlib.sha256(f.read()).hexdigest()


def forbidden_keys_in(node, found=None):
    found = [] if found is None else found
    if isinstance(node, dict):
        for k, v in node.items():
            if k.lower() in {'qty', 'quantity', 'avgprice', 'invested', 'value', 'prices',
                             'currentprice', 'purchases', 'ledger', 'cost'}:
                found.append(k)
            forbidden_keys_in(v, found)
    elif isinstance(node, list):
        for v in node:
            forbidden_keys_in(v, found)
    return found


class TestLedgerParsing(unittest.TestCase):
    def test_valid_ledger(self):
        text = '﻿# commento\r\n\r\n' + '\r\n'.join(
            r + '   # nota' for r in base_rows()) + '\r\n'
        led = pu.parse_ledger(text, TODAY)
        self.assertEqual(len(led['personal']), 3)
        self.assertEqual(led['family'], [('2026-04-01', 'EUNL', 20, 100.0)])
        self.assertEqual(led['personal'][2], ('2026-03-05', 'EUNL', 100, 50.0))

    def test_spaces_around_fields_accepted(self):
        led = pu.parse_ledger(' personal , 2026-01-05 , UST , 10 , 100.00 \n family,2026-04-01,EUNL,20,100.00', TODAY)
        self.assertEqual(led['personal'][0], ('2026-01-05', 'UST', 10, 100.0))

    def test_invalid_cases_fail_without_leaking_content(self):
        """Ogni caso contiene il testo CANARY nel punto sbagliato: il messaggio non deve riportarlo."""
        ok_fam = row('family', '2026-04-01', 'EUNL', '20', '100.00')
        bad = {
            'secret mancante (None)': None,
            'vuoto': '',
            'solo spazi': '  \n \t ',
            'solo commenti': '# ' + CANARY + '\n',
            'troppo pochi campi': 'personal,2026-01-05,UST,' + CANARY,
            'troppi campi': row('personal', '2026-01-05', 'UST', '10', '100.00') + ',' + CANARY,
            'portafoglio sconosciuto': row(CANARY, '2026-01-05', 'UST', '10', '100.00'),
            'data formato errato': row('personal', CANARY, 'UST', '10', '100.00'),
            'data inesistente': row('personal', '2026-02-30', 'UST', '10', '100.00'),
            'data con slash': row('personal', '2026/01/05', 'UST', '10', '100.00'),
            'data nel futuro': row('personal', '2026-10-18', 'UST', '10', '100.00'),
            'data prima del 2000': row('personal', '1999-12-31', 'UST', '10', '100.00'),
            'ticker sconosciuto': row('personal', '2026-01-05', CANARY, '10', '100.00'),
            'ticker minuscolo': row('personal', '2026-01-05', 'ust', '10', '100.00'),
            'quantita testo': row('personal', '2026-01-05', 'UST', CANARY, '100.00'),
            'quantita zero': row('personal', '2026-01-05', 'UST', '0', '100.00'),
            'quantita negativa': row('personal', '2026-01-05', 'UST', '-3', '100.00'),
            'quantita decimale': row('personal', '2026-01-05', 'UST', '1.5', '100.00'),
            'quantita notazione scientifica': row('personal', '2026-01-05', 'UST', '1e3', '100.00'),
            'quantita enorme': row('personal', '2026-01-05', 'UST', '99999999', '100.00'),
            'prezzo testo': row('personal', '2026-01-05', 'UST', '10', CANARY),
            'prezzo zero': row('personal', '2026-01-05', 'UST', '10', '0'),
            'prezzo negativo': row('personal', '2026-01-05', 'UST', '10', '-5'),
            'prezzo con virgola decimale': row('personal', '2026-01-05', 'UST', '10', '12,5') ,
            'prezzo notazione scientifica': row('personal', '2026-01-05', 'UST', '10', '1e2'),
            'prezzo vuoto': row('personal', '2026-01-05', 'UST', '10', ''),
            'carattere nullo': row('personal', '2026-01-05', 'UST', '10', '100.00') + '\x00' + CANARY,
        }
        for name, text in bad.items():
            if text is not None and name not in ('solo commenti', 'vuoto', 'solo spazi', 'secret mancante (None)',
                                                 'carattere nullo'):
                text = text + '\n' + ok_fam
            with self.subTest(caso=name):
                with self.assertRaises(pu.LedgerError) as cm:
                    pu.parse_ledger(text, TODAY)
                msg = str(cm.exception)
                self.assertNotIn(CANARY, msg)
                for secret_like in ('100.00', '12,5', '99999999'):
                    self.assertNotIn(secret_like, msg)

    def test_errors_report_line_number(self):
        text = ledger_text(base_rows()[0], row('personal', '2026-01-05', 'UST', 'x', '100.00'))
        with self.assertRaises(pu.LedgerError) as cm:
            pu.parse_ledger(text, TODAY)
        self.assertIn('riga 2', str(cm.exception))
        self.assertIn('quantita', str(cm.exception))

    def test_portfolio_without_rows_is_incomplete(self):
        only_personal = ledger_text(*base_rows()[:3])
        with self.assertRaises(pu.LedgerError) as cm:
            pu.parse_ledger(only_personal, TODAY)
        self.assertIn('family', str(cm.exception))

    def test_size_limits(self):
        with self.assertRaises(pu.LedgerError):
            pu.parse_ledger('#' * (pu.MAX_LEDGER_CHARS + 1), TODAY)
        many = ledger_text(*(base_rows() * (pu.MAX_LEDGER_ROWS // 4 + 2)))
        with self.assertRaises(pu.LedgerError):
            pu.parse_ledger(many, TODAY)


class TestCalculations(unittest.TestCase):
    def setUp(self):
        self.ledger = pu.parse_ledger(ledger_text(*base_rows()), TODAY)
        self.pub = pu.build_public_portfolio(self.ledger, BASE_PRICES, TODAY)

    def test_known_values(self):
        p = self.pub['personal']
        self.assertEqual(p['holdings']['UST']['perf_pct'], 18.18)     # costo medio 110, prezzo 130
        self.assertEqual(p['holdings']['EUNL']['perf_pct'], -20.0)    # costo medio 50, prezzo 40
        self.assertEqual(self.pub['family']['performance_pct'], -60.0)

    def test_aggregate_is_weighted_not_mean_of_percentages(self):
        p = self.pub['personal']
        self.assertEqual(p['performance_pct'], -8.33)                 # (6600-7200)/7200
        mean_of_pct = (18.18 + -20.0) / 2
        self.assertNotAlmostEqual(p['performance_pct'], mean_of_pct, places=1)

    def test_weights(self):
        w = self.pub['personal']['holdings']
        self.assertEqual(w['UST']['weight_pct'], 39.39)
        self.assertEqual(w['EUNL']['weight_pct'], 60.61)
        self.assertAlmostEqual(sum(h['weight_pct'] for h in w.values()), 100, delta=0.02)
        self.assertEqual(self.pub['family']['holdings']['EUNL']['weight_pct'], 100.0)

    def test_etf_without_purchases_is_absent(self):
        self.assertNotIn('VWCE', self.pub['personal']['holdings'])
        self.assertNotIn('IS3R', self.pub['family']['holdings'])

    def test_history_is_deterministic_and_ends_on_current_performance(self):
        h = self.pub['personal']['history']
        self.assertEqual(h[0]['m'], '2026-01')
        self.assertEqual(h[-1], {'m': '2026-10', 'pct': self.pub['personal']['performance_pct']})
        again = pu.build_public_portfolio(self.ledger, BASE_PRICES, TODAY)
        self.assertEqual(h, again['personal']['history'])

    def test_public_output_is_scale_free(self):
        """Moltiplicando tutte le quantita' per 3 il JSON pubblico non cambia: nessuna dimensione assoluta e' pubblicata."""
        scaled = [r.split(',') for r in base_rows()]
        for r in scaled:
            r[3] = str(int(r[3]) * 3)
        led3 = pu.parse_ledger(ledger_text(*[','.join(r) for r in scaled]), TODAY)
        pub3 = pu.build_public_portfolio(led3, BASE_PRICES, TODAY)
        self.assertEqual(json.dumps(pub3, sort_keys=True), json.dumps(self.pub, sort_keys=True))

    def test_public_schema_valid_and_free_of_private_fields(self):
        pu.validate_public_portfolio(self.pub)
        self.assertEqual(forbidden_keys_in(self.pub), [])
        self.assertEqual(set(self.pub), {'personal', 'family', 'last_update'})


class TestSchemaValidation(unittest.TestCase):
    def good(self):
        led = pu.parse_ledger(ledger_text(*base_rows()), TODAY)
        return pu.build_public_portfolio(led, BASE_PRICES, TODAY)

    def test_rejects_unexpected_or_malformed_fields(self):
        cases = {}
        a = self.good(); a['personal']['holdings']['UST']['qty'] = 1; cases['campo extra nel titolo'] = a
        b = self.good(); b['prices'] = {}; cases['chiave extra alla radice'] = b
        c = self.good(); c['personal']['invested'] = 1; cases['campo extra nel portafoglio'] = c
        d = self.good(); d['family']['holdings']['EUNL']['weight_pct'] = '100'; cases['peso come testo'] = d
        e = self.good(); e['personal']['performance_pct'] = float('nan'); cases['NaN'] = e
        f = self.good(); f['personal']['history'][0]['m'] = '2026-13'; cases['mese non valido'] = f
        g = self.good(); g['personal']['holdings']['XXXX'] = {'perf_pct': 1, 'weight_pct': 1}; cases['ticker sconosciuto'] = g
        h = self.good(); del h['family']; cases['portafoglio mancante'] = h
        i = self.good(); i['personal']['history'][0]['value'] = 5; cases['campo extra nello storico'] = i
        j = self.good(); j['personal']['holdings']['UST']['weight_pct'] = 150; cases['peso oltre 100'] = j
        for name, data in cases.items():
            with self.subTest(caso=name):
                with self.assertRaises(pu.SchemaError):
                    pu.validate_public_portfolio(data)


class TestUpdateFile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, 'data.json')
        legacy = {'km_base': 1.5, 'km_strava': 2.5, 'km_last_update': '2026-01-01',
                  'portfolio': {'personal': {'performance_pct': 1.0, 'holdings': {'UST': {'qty': 3, 'avgPrice': 1.0}}},
                                'prices': {'UST': 1.0}, 'last_update': '2026-01-01'}}
        with open(self.path, 'w') as f:
            json.dump(legacy, f, indent=2)
        self.ledger = pu.parse_ledger(ledger_text(*base_rows()), TODAY)

    def tmp_leftovers(self):
        return [n for n in os.listdir(self.tmp.name) if n != 'data.json']

    def test_update_replaces_portfolio_and_keeps_other_fields(self):
        pu.update_data_file(self.path, self.ledger, BASE_PRICES, TODAY)
        data = load_json(self.path)
        self.assertEqual((data['km_base'], data['km_strava'], data['km_last_update']), (1.5, 2.5, '2026-01-01'))
        self.assertEqual(forbidden_keys_in(data), [])      # i campi riservati del vecchio formato sono spariti
        pu.validate_public_portfolio(data['portfolio'])
        self.assertEqual(self.tmp_leftovers(), [])

    def test_missing_file_is_created(self):
        os.unlink(self.path)
        pu.update_data_file(self.path, self.ledger, BASE_PRICES, TODAY)
        self.assertEqual(set(load_json(self.path)), {'portfolio'})

    def test_failures_leave_file_untouched(self):
        before = sha(self.path)
        injected = dict(self.ledger)
        scenarios = {
            'schema violato dopo il calcolo': mock.patch.object(
                pu, 'build_public_portfolio', return_value={'personal': {}, 'family': {}, 'last_update': str(TODAY)}),
            'errore imprevisto nel calcolo': mock.patch.object(
                pu, 'build_public_portfolio', side_effect=ZeroDivisionError(CANARY)),
            'scrittura interrotta': mock.patch.object(pu.os, 'replace', side_effect=OSError(CANARY)),
        }
        for name, patcher in scenarios.items():
            with self.subTest(caso=name), patcher:
                with self.assertRaises(Exception):
                    pu.update_data_file(self.path, injected, BASE_PRICES, TODAY)
                self.assertEqual(sha(self.path), before)
                self.assertEqual(self.tmp_leftovers(), [], 'file temporanei lasciati nella cartella')

    def test_corrupt_existing_json_is_not_overwritten(self):
        with open(self.path, 'w') as f:
            f.write('{"km_base": ')
        before = sha(self.path)
        with self.assertRaises(ValueError):
            pu.update_data_file(self.path, self.ledger, BASE_PRICES, TODAY)
        self.assertEqual(sha(self.path), before)


class TestMainAndLogs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, 'data.json')
        with open(self.path, 'w') as f:
            json.dump({'km_base': 1.0, 'portfolio': {'last_update': '2026-01-01'}}, f)
        self.before = sha(self.path)

    def run_main(self, env_value, prices=None):
        out, err = io.StringIO(), io.StringIO()
        env = {} if env_value is None else {pu.LEDGER_ENV: env_value}
        with mock.patch.dict(os.environ, env, clear=False), \
                mock.patch.object(pu, 'fetch_prices', return_value=prices or BASE_PRICES), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            if env_value is None:
                os.environ.pop(pu.LEDGER_ENV, None)
            rc = pu.main(self.path)
        return rc, out.getvalue(), err.getvalue()

    def canary_rows(self):
        return [
            row('personal', '2026-01-05', 'UST', CANARY_QTY, CANARY_PRICE),
            row('personal', '2026-02-05', 'EUNL', '5', '100.00'),
            row('family', '2026-04-01', 'EUNL', '20', '100.00'),
        ]

    def test_success_prints_and_writes_no_ledger_values(self):
        rc, out, err = self.run_main(ledger_text(*self.canary_rows()))
        self.assertEqual(rc, 0, err)
        written = read_text(self.path)
        for text in (out, err, written):
            self.assertNotIn(CANARY_QTY, text)
            self.assertNotIn(CANARY_PRICE, text)
        pu.validate_public_portfolio(json.loads(written)['portfolio'])

    def test_missing_or_empty_secret_fails_and_keeps_data(self):
        for value in (None, '', '   \n'):
            with self.subTest(valore=repr(value)):
                rc, out, err = self.run_main(value)
                self.assertEqual(rc, 1)
                self.assertIn(pu.LEDGER_ENV, err)
                self.assertEqual(sha(self.path), self.before)

    def test_malformed_secret_fails_keeps_data_and_does_not_echo_it(self):
        text = ledger_text(*self.canary_rows()) + row('personal', '2026-01-05', 'UST', CANARY + CANARY_QTY, '5.00') + '\n'
        rc, out, err = self.run_main(text)
        self.assertEqual(rc, 1)
        self.assertEqual(sha(self.path), self.before)
        for leaked in (CANARY, CANARY_QTY, CANARY_PRICE):
            self.assertNotIn(leaked, out + err)
        self.assertIn('riga 4', err)

    def test_malformed_secret_stops_before_any_network_access(self):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {pu.LEDGER_ENV: 'non valido'}), \
                mock.patch.object(pu, 'fetch_prices') as fetch, \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(pu.main(self.path), 1)
        fetch.assert_not_called()

    def test_unexpected_error_hides_details_and_traceback(self):
        err_buf, out_buf = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {pu.LEDGER_ENV: ledger_text(*self.canary_rows())}), \
                mock.patch.object(pu, 'fetch_prices', return_value=BASE_PRICES), \
                mock.patch.object(pu, 'build_public_portfolio',
                                  side_effect=ValueError(CANARY + CANARY_QTY + CANARY_PRICE)), \
                contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
            rc = pu.main(self.path)
        text = out_buf.getvalue() + err_buf.getvalue()
        self.assertEqual(rc, 1)
        self.assertEqual(sha(self.path), self.before)
        self.assertIn('ValueError', text)
        for leaked in (CANARY, CANARY_QTY, CANARY_PRICE, 'Traceback'):
            self.assertNotIn(leaked, text)

    def test_cli_process_fails_cleanly_without_secret(self):
        env = {k: v for k, v in os.environ.items() if k != pu.LEDGER_ENV}
        res = subprocess.run([sys.executable, '-I', SCRIPT], cwd=self.tmp.name, env=env,
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(res.returncode, 1)
        self.assertNotIn('Fetching', res.stdout)       # nessun accesso alla rete
        self.assertEqual(sha(self.path), self.before)
        self.assertEqual([n for n in os.listdir(self.tmp.name) if n != 'data.json'], [])

    def test_cli_process_malformed_secret_no_leak_in_streams(self):
        env = dict(os.environ)
        env[pu.LEDGER_ENV] = ledger_text(*self.canary_rows()) + CANARY + ',' + CANARY_QTY + '\n'
        res = subprocess.run([sys.executable, '-I', SCRIPT], cwd=self.tmp.name, env=env,
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(res.returncode, 1)
        for leaked in (CANARY, CANARY_QTY, CANARY_PRICE, 'Traceback'):
            self.assertNotIn(leaked, res.stdout + res.stderr)
        self.assertEqual(sha(self.path), self.before)


class TestWorkflowSimulation(unittest.TestCase):
    """Simula in locale la sequenza del workflow: se lo script fallisce, il commit non parte."""

    def test_commit_step_not_reached_when_update_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = os.path.join(tmp, 'commit-eseguito')
            script = f'set -e\n"{sys.executable}" -I "{SCRIPT}"\ntouch "{marker}"\n'
            env = {k: v for k, v in os.environ.items() if k != pu.LEDGER_ENV}
            res = subprocess.run(['bash', '-c', script], cwd=tmp, env=env, capture_output=True, text=True, timeout=60)
            self.assertNotEqual(res.returncode, 0)
            self.assertFalse(os.path.exists(marker))


if __name__ == '__main__':
    unittest.main()
