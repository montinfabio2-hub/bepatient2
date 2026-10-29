"""Test di tools/check_privacy.py: deve trovare le esposizioni vere e NON dare falsi positivi.

Tutti i dati sono fittizi. I numeri dei registri di prova sono scritti come stringhe
(o calcolati) per non far scattare il controllo sui file di test stessi.
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import date
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'tools'))
import portfolio_update as pu  # noqa: E402
import check_privacy as cp  # noqa: E402

TODAY = date(2026, 10, 17)


def row(pf, d, t, q, p):
    return ','.join([pf, d, t, q, p])


FAKE = '\n'.join([
    row('personal', '2026-01-05', 'EUNL', '10', '100.00'),
    row('personal', '2026-02-05', 'EUNL', '5', '110.50'),
    row('family', '2026-03-05', 'VWCE', '3', '130.25'),
]) + '\n'
FP = cp.ledger_fingerprints(pu.parse_ledger(FAKE, TODAY))   # EUNL: totale 15, capitale 1552.50, medio 103.50


def l2(text):
    return cp.scan_text_level2('x.txt', text, FP)


def codes(findings):
    return {f[2] for f in findings}


def read_text(path):
    with open(path, encoding='utf-8') as f:
        return f.read()


class TestLevel2Detects(unittest.TestCase):
    """Fughe vere, in formati diversi: devono essere trovate."""

    def test_row_leak_in_many_formats(self):
        for p in ('110.5', '110.50'):                       # formattazioni diverse dello stesso prezzo
            q = '5'
            formats = {
                'oggetto JS': "{ticker:'EUNL', qty:%s, price:%s}",
                'JSON': '{"quantity": %s, "avg": %s}',
                'tupla Python': '("2026-02-05","EUNL",%s,%s)',
                'riga CSV': 'personal,2026-02-05,EUNL,%s,%s',
                'celle HTML': '<td>%s</td><td>%s</td>',
                'su righe diverse': '%s\n\n   \n%s',
                'senza ticker': '[%s, %s]',
            }
            for name, fmt in formats.items():
                with self.subTest(formato=name, prezzo=p):
                    self.assertIn('L2-riga-registro', codes(l2(fmt % (q, p))))

    def test_totals_and_average(self):
        self.assertIn('L2-quantita-totale', codes(l2('Totale EUNL: 15 quote')))
        self.assertIn('L2-capitale', codes(l2('capitale 1552.50')))
        self.assertIn('L2-capitale', codes(l2('capitale 1552.5')))
        self.assertIn('L2-prezzo-medio', codes(l2('EUNL prezzo medio 103.50')))
        self.assertIn('L2-prezzo-medio', codes(l2('EUNL avg 103.5')))

    def test_report_gives_line_number(self):
        found = l2('riga uno\nriga due\nEUNL totale 15\n')
        self.assertEqual([f[1] for f in found], [3])


class TestLevel2NoFalsePositives(unittest.TestCase):
    """Coincidenze innocue: NON devono essere segnalate."""

    def test_single_coincidences_are_ignored(self):
        benign = {
            'solo la quantita': 'Ci sono 5 elementi e 3 gruppi, 10 prove.',
            'solo il prezzo': 'Il prezzo 110.50 EUR non e legato a nessun acquisto.',
            'quantita e prezzo lontani': '5 ' + 'x ' * 300 + ' 110.5',
            'prezzo dentro un numero piu lungo': 'valori 5 e 1110.50 e 110.505',
            'prezzo intero senza decimali': 'sono 10 e 100 in elenco',
            'unita CSS': 'font-size:11px;letter-spacing:0.12em;margin:5px;width:110px',
            'quantita totale senza ticker': 'in totale 15 voci',
            'quantita totale lontano dal ticker': 'EUNL ' + 'a ' * 400 + ' 15',
            'numero qualunque a qualche decina di caratteri dal ticker': 'EUNL ' + 'a ' * 30 + ' 15',
            'parti di una data': "EUNL 2026-01-15 e 2026-02-15",
            'prezzo medio senza ticker': 'valore 103.50 in tabella',
            'coordinate SVG': '<polyline points="5 110.5 5 110.5 5 110.5"/><path d="M5 110.5 L5 110.5"/>',
            'codice ISIN e URL': "isin:'IE00B4L5Y983', link:'https://esempio.it/251882/prodotto'",
        }
        for name, text in benign.items():
            with self.subTest(caso=name):
                self.assertEqual(l2(text), [])

    def test_current_repository_is_clean_with_an_unrelated_ledger(self):
        """Con un registro di prova mai usato nel repo non devono esserci segnalazioni su file reali."""
        price = '%.2f' % (9337 / 100)          # numeri calcolati: non compaiono nei file
        text = row('personal', '2026-01-05', 'EUNL', str(8 + 9), price) + '\n' + \
            row('family', '2026-02-05', 'UST', str(11 + 12), '%.2f' % (6411 / 100)) + '\n'
        ledger = pu.parse_ledger(text, TODAY)
        findings, scanned = cp.scan_root(ROOT, ledger)
        self.assertGreater(scanned, 3)
        self.assertEqual(findings, [])


class TestLevel1(unittest.TestCase):
    def test_ledger_rows_are_found_except_marked_examples(self):
        leak = row('personal', '2026-01-05', 'UST', '10', '100.00')
        self.assertIn('L1-riga-registro', codes(cp.scan_text_level1('a.txt', leak + '\n')))
        self.assertEqual(cp.scan_text_level1('a.txt', leak + '   # ESEMPIO-FITTIZIO\n'), [])

    def test_purchase_records_in_code_are_found(self):
        recs = [
            "{ticker:'UST', qty:14, price:61.35}",
            '("2024-05-06","UST",27,88.15)',
            '{"ticker": "EUNL", "n": 9, "p": 77.85}',
        ]
        for text in recs:
            with self.subTest(testo=text):
                self.assertEqual(codes(cp.scan_text_level1('a.py', text)), {'L1-record-acquisto'})

    def test_benign_code_and_prose_are_not_flagged(self):
        benign = {
            'riga dei metadati': ("{ name:'Amundi Core Nasdaq-100 Swap UCITS ETF Acc', isin:'LU1829221024', "
                                  "ticker:'UST', link:'https://esempio.it/251882/x?switchLocale=y', yahooSymbol:'UST.PA' },"),
            'ticker con solo decimale': "EUNL costa 0.20% all'anno",
            'ticker con solo intero': 'UST ha 100 posizioni',
            'numeri senza ticker': 'const a = 10, b = 126.4;',
            'minuscolo come parola comune': 'la parola ust 10 126.4 non e un ticker',
            'sigle dentro altre parole': 'EUNLX 10 126.4',
        }
        for name, text in benign.items():
            with self.subTest(caso=name):
                self.assertEqual(cp.scan_text_level1('a.html', text), [])
        # prosa e documentazione: l'euristica "ticker + intero + decimale" non si applica
        self.assertEqual(cp.scan_text_level1('docs/nota.md', 'UST 10 126.4'), [])

    def test_json_forbidden_keys_any_depth_any_case(self):
        for doc in ({'Qty': 1}, {'a': {'b': [{'avgPrice': 1}]}}, {'x': {'INVESTED': 3}}, {'prices': {}}):
            with self.subTest(doc=doc):
                self.assertIn('L1-chiave-vietata', codes(cp.scan_json('altro.json', json.dumps(doc))))

    def test_json_allowed_content_is_clean(self):
        self.assertEqual(cp.scan_json('altro.json', json.dumps({'km_base': 1.0, 'nota': 'ok'})), [])
        self.assertEqual(cp.scan_json('data.json', read_text(os.path.join(ROOT, 'data.json'))), [])

    def test_data_json_schema_violations(self):
        good = json.loads(read_text(os.path.join(ROOT, 'data.json')))
        for name, mutate in {
            'chiave extra nel portafoglio': lambda d: d['portfolio']['personal'].update(extra=1),
            'peso come testo': lambda d: d['portfolio']['family']['holdings']['EUNL'].update(weight_pct='100'),
            'blocco mancante': lambda d: d['portfolio'].pop('family'),
        }.items():
            with self.subTest(caso=name):
                doc = json.loads(json.dumps(good))
                mutate(doc)
                self.assertIn('L1-schema', codes(cp.scan_json('data.json', json.dumps(doc))))

    def test_invalid_json_is_reported(self):
        self.assertEqual(codes(cp.scan_json('rotto.json', '{"a": ')), {'JSON-non-valido'})

    def test_repository_files_are_clean_at_level_1(self):
        findings, scanned = cp.scan_root(ROOT)
        self.assertGreater(scanned, 3)
        self.assertEqual(findings, [])


class TestCommandLine(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ledger_file = os.path.join(self.tmp.name, 'registro.csv')
        self.root = os.path.join(self.tmp.name, 'sito')
        os.makedirs(self.root)
        with open(self.ledger_file, 'w') as f:
            f.write(FAKE)

    def put(self, name, text):
        with open(os.path.join(self.root, name), 'w') as f:
            f.write(text)

    def run_cli(self, *extra, env=None):
        out, err = io.StringIO(), io.StringIO()
        base = {k: v for k, v in os.environ.items() if k != pu.LEDGER_ENV}
        base.update(env or {})
        with mock.patch.dict(os.environ, base, clear=True), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cp.main(['--root', self.root, *extra])
        return rc, out.getvalue(), err.getvalue()

    def test_clean_site_exit_0(self):
        self.put('index.html', '<p>Nessun dato riservato, solo il 39.88% e il 6.78%</p>')
        rc, out, _ = self.run_cli('--ledger-file', self.ledger_file)
        self.assertEqual(rc, 0)
        self.assertIn('livelli 1+2', out)

    def test_leak_exit_1_and_report_never_shows_values(self):
        self.put('app.js', "var acquisto = {ticker:'EUNL', qty:5, price:110.5};\n")
        self.put('ok.html', '<p>ciao</p>')
        rc, out, err = self.run_cli('--ledger-file', self.ledger_file)
        self.assertEqual(rc, 1)
        self.assertIn('app.js:1', out)
        self.assertNotIn('ok.html', out)
        for value in ('110.5', '110.50', 'qty:5', '1552', '103.5'):
            self.assertNotIn(value, out + err)

    def test_ledger_from_environment_variable(self):
        self.put('app.js', "x = ['EUNL', 5, 110.5];\n")
        rc, out, _ = self.run_cli(env={pu.LEDGER_ENV: FAKE})
        self.assertEqual(rc, 1)
        self.assertIn('L2-riga-registro', out)

    def test_without_ledger_only_level_1(self):
        self.put('index.html', '<p>ok</p>')
        rc, out, _ = self.run_cli()
        self.assertEqual(rc, 0)
        self.assertIn('solo livello 1', out)

    def test_invalid_or_unreadable_ledger_exit_2_without_echo(self):
        with open(self.ledger_file, 'w') as f:
            f.write('personal,2026-01-05,EUNL,CANARY,100.00\n')
        rc, out, err = self.run_cli('--ledger-file', self.ledger_file)
        self.assertEqual(rc, 2)
        self.assertNotIn('CANARY', out + err)
        rc, _, _ = self.run_cli('--ledger-file', os.path.join(self.tmp.name, 'manca.csv'))
        self.assertEqual(rc, 2)

    def test_binary_files_are_skipped(self):
        with open(os.path.join(self.root, 'img.webp'), 'wb') as f:
            f.write(b'\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01')
        rc, out, _ = self.run_cli('--ledger-file', self.ledger_file)
        self.assertEqual(rc, 0)


if __name__ == '__main__':
    unittest.main()
