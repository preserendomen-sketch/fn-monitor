# fn-monitor

Monitor čakajočih nalogov in skener priložnosti, ki tečeta na GitHub Actions. Nadomestita procesa, ki sta se na strežniku zmrzovala.

## Kaj počne

- **Monitor** (`monitor.py`) vsakih 60 sekund preveri čakajoče naloge in pošlje webhook ob dogodkih `near_entry`, `triggered`, `m15_close_against`, `near_sl`, `sl_hit`, `tp_hit` in `halfway_to_tp`.
- **Skener** (`scanner.py`) vsakih 5 minut pregleda deset simbolov (EURUSD, GBPUSD, AUDUSD, USDJPY, XAUUSD, USOUSD, BTCUSD, NAS100, GER40, US30) in javi kandidate `breakout`, `retest` in `exhaustion`. USOUSD je surova nafta WTI.
- Oba tečeta v istem zagonu približno 25 minut. Razpored je na vsakih 30 minut ob delavnikih, približno od 07:00 do 21:00 po času Europe/Ljubljana. Cron je v UTC (pozimi UTC+1, torej 06:00–20:00 UTC). Poleti, ko je Ljubljana UTC+2, isto okno pade približno eno uro pozneje. Zamuda načrtovanega zagona je v redu.
- Stanje (že poslani dogodki, cooldowni, dnevne omejitve) se med zagoni shrani v predpomnilnik Actions, da se isto opozorilo ne pošlje dvakrat. Skener sme na dan poslati največ **2 kandidata na simbol** in **8 skupaj**.
- BTCUSD je samo v skenerju. Med nalogi ga ni, ker ga je posrednik zavrnil.

Cene za Yahoo Finance se najprej prenesejo s `curl_cffi` (posnemanje brskalnika Chrome). Če Yahoo zavrne IP GitHubovega tekača, sledi navaden `urllib`, nato še `query2.finance.yahoo.com`. BTC pride z Binance; če je `api.binance.com` blokiran, se uporabi `data-api.binance.vision`.

Če skrivnosti manjkata, se v dnevnik zapiše opozorilo in pošiljanje se preskoči. Program se zaradi tega ne ustavi. Naslov in ključ se v dnevnik ne izpisujeta.

## Skrivnosti

Webhook naslov in ključ ne sodita v git. Dodaj ju kot skrivnosti repozitorija:

1. Odpri repozitorij na GitHubu.
2. **Settings → Secrets and variables → Actions**.
3. **New repository secret**.
4. Ime: `FN_WEBHOOK_URL`. Vrednost: polni URL webhooka.
5. Še enkrat **New repository secret**.
6. Ime: `FN_WEBHOOK_KEY`. Vrednost: ključ (brez besede Bearer).
7. Ključ se pošlje v glavi `Authorization: Bearer <ključ>`.

## Urejanje nalogov

Aktivni nalogi so v datoteki `config.json`, v polju `orders`. Trenutno so trije: EURUSD, AUDUSD in XAUUSD (vsi `sell`). BTCUSD v to polje ne dodajaj. V `scanner.universe` BTCUSD ostane.

Urejanje prek spletnega vmesnika GitHub:

1. V repozitoriju odpri `config.json`.
2. Klikni ikono svinčnika (**Edit this file**).
3. V `orders` popravi `side` (`buy` ali `sell`), `entry`, `sl`, `tp` in `offset`, ali dodaj nov objekt z polji `id`, `symbol`, `source`, `ticker`, `offset`, `side`, `entry`, `sl`, `tp`.
4. Spodaj izberi **Commit changes**, veja naj bo `main`, in potrdi.

Naslednji načrtovani ali ročni zagon prebere novo datoteko. V `config.json` ne vpisuj webhook naslova ali ključa.

## Ročni zagon

1. Odpri zavihek **Actions**.
2. Levo izberi tok **Monitor**.
3. Klikni **Run workflow**, veja `main`.
4. Po želji nastavi trajanje v minutah (privzeto 25, največ 30) in potrdi **Run workflow**.
