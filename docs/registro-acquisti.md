# Registro acquisti (privato)

Quantita', prezzi di acquisto e date degli acquisti **non stanno nella repository**: sono
nel Secret di GitHub Actions `PORTFOLIO_LEDGER`. Il sito pubblica solo percentuali.

## Formato

Una riga per acquisto, cinque campi separati da virgola:

```
portafoglio,data,ticker,quantita,prezzo
```

| Campo | Regola |
|---|---|
| portafoglio | `personal` oppure `family` |
| data | `AAAA-MM-GG`, non nel futuro |
| ticker | `UST`, `EUNL`, `VWCE` oppure `IS3R` |
| quantita | numero intero positivo |
| prezzo | numero positivo con il **punto** decimale (non la virgola) |

Righe vuote ignorate. Tutto cio' che segue `#` e' un commento. Ogni portafoglio deve avere
almeno un acquisto. Esempio con **dati inventati**:

```
personal,2026-01-15,EUNL,10,100.00   # ESEMPIO-FITTIZIO
personal,2026-02-20,VWCE,5,120.50    # ESEMPIO-FITTIZIO
family,2026-03-10,EUNL,20,101.25     # ESEMPIO-FITTIZIO
```

## Aggiungere o modificare un acquisto

GitHub **non permette di rileggere** il valore di un Secret: si puo' solo sostituirlo.
Per questo serve una copia "master" del registro, conservata da te in un posto privato
(gestore di password o nota cifrata), **mai nella repository**.

1. Apri la tua copia master e aggiungi (o correggi) la riga.
2. GitHub, repository `bepatient` → *Settings* → *Secrets and variables* → *Actions*.
3. Sul Secret `PORTFOLIO_LEDGER` scegli *Update* (matita), incolla **tutto** il registro, salva.
4. Per applicarlo subito: *Actions* → *Update Site Data* → *Run workflow*.
   Altrimenti viene usato alla prossima esecuzione automatica.

Un acquisto dimenticato non genera errori: il rendimento risulta calcolato sui soli acquisti
presenti. Dopo ogni modifica controlla che il numero di righe incollate sia quello atteso.

## Errori

Se il Secret manca, e' vuoto o contiene righe non valide, il workflow **fallisce senza
modificare `data.json`**. Il messaggio indica numero di riga e campo (ad esempio
`riga 12: prezzo non valido`), mai il contenuto. Se il workflow non usa il Secret
corretto, `data.json` resta com'e'.
