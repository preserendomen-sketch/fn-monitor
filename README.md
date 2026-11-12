# fn-monitor

Monitor čakajočih nalogov in skener priložnosti, ki tečeta na GitHub Actions. Nadomestita procesa, ki sta se na strežniku zmrzovala.

## Kaj počne

- **Monitor** (`monitor.py`) vsakih 60 sekund preveri čakajoče naloge in pošlje webhook ob dogodkih `near_entry`, `triggered`, `m15_close_against`, `near_sl`, `sl_hit`, `tp_hit` in `halfway_to_tp`.
- **Skener** (`scanner.py`) vsakih 5 minut pregleda deset simbolov (EURUSD, GBPUSD, AUDUSD, USDJPY, XAUUSD, USOUSD, BTCUSD, NAS100, GER40, US30) in javi kandidate `breakout`, `retest` in `exhaustion`. USOUSD je surova nafta WTI.
- Oba tečeta v istem zagonu približno 28 minut. Razpored je na vsakih 15 minut ob delavnikih, od 06:00 do 20:45 UTC (pozimi približno 07:00–21:45 po času Europe/Ljubljana, poleti uro pozneje). Tek, ki že teče, se ne prekine. Če GitHub zamudi, skener ob naslednjem zagonu še pošlje sveče, ki so se zaprle v zadnjih 45 minutah; starejše samo zapiše kot `zamujen`.
- Na VPS (`C:\fn-monitor`) isti skener teče neprekinjeno, tudi ponoči in ob koncu tedna, da ostanejo sveče sveže. Vhodna opozorila se še vedno pošljejo samo ob delavnikih od 08:00 do 21:00 po času Europe/Ljubljana. Razpored GitHub Actions se ne spremeni.
- Stanje (že poslani dogodki, cooldowni, dnevne omejitve) se med zagoni shrani v predpomnilnik Actions, da se isto opozorilo ne pošlje dvakrat. Skener sme na dan poslati največ **2 kandidata na simbol** in **12 skupaj**.
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

Aktivni nalogi so v datoteki `config.json`, v polju `orders`. Trenutno je seznam prazen. BTCUSD v to polje ne dodajaj. V `scanner.universe` BTCUSD ostane.

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
4. Po želji nastavi trajanje v minutah (privzeto 28, največ 30) in potrdi **Run workflow**.

## Skener na Windows VPS

Mapa na strežniku je `C:\fn-monitor`. Python je `C:\fn-monitor\.venv\Scripts\python.exe`. Strežnik nima gita: posodobljene datoteke (`scanner.py` in mapa `windows\`) prekopiraj ročno. `config.json` pri tej spremembi ne potrebuje novih polj. Pragovi (RVOL, cooldown, dnevne omejitve, okno 08:00–21:00) ostanejo, kot so.

Skener teče ves čas, tudi ponoči, ob koncu tedna in kadar nihče ni prijavljen. Tako ostanejo sveče sveže, tudi BTC, ki se trguje 24 ur. Vhodno opozorilo se pošlje samo, če se je sveča zaprla ob delavniku od 08:00 do 21:00 po času Europe/Ljubljana. Zaprtje ob 21:00 šteje že zunaj okna. Sveča, ki se je zaprla znotraj okna, se lahko pošlje še do 45 minut pozneje, če je bil proces medtem ustavljen. To je isti zamik kot doslej. Sveča, ki se zapre ponoči ali ob soboti in nedelji, se ne pošlje.

Odjava iz strežnika skenerja ne ustavi. Enako velja za zaprtje oddaljenega namizja.

### Priprava

1. Namesti Python 3.12. V `C:\fn-monitor` ustvari okolje in namesti odvisnosti. `tzdata` je v `requirements.txt` in je na Windowsu potreben za časovni pas `Europe/Ljubljana`:

   ```
   py -3.12 -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt
   ```

2. Naslov in ključ nastavi kot trajni uporabniški spremenljivki računa, ki bo lastnik opravila. Ne v `config.json` in ne v git:
   - `FN_WEBHOOK_URL` je polni URL webhooka
   - `FN_WEBHOOK_KEY` je ključ, brez besede Bearer

   Nastavi ju v lastnostih sistema (spremenljivke uporabnika) ali z `setx`. Opravilo, ki teče brez prijave, naloži profil tega uporabnika in ju vidi. Spremenljivka, ki obstaja samo v odprtem oknu ukazne vrstice, ne zadošča.

### Zagon ob vklopu

Namestitev zamenja obstoječe opravilo **FN Scanner** (prej samo ob prijavi) z opravilom, ki se zažene ob vklopu in teče, tudi če nihče ni prijavljen.

1. Odpri PowerShell kot ta uporabnik.
2. Zaženi:

   ```
   powershell -NoProfile -ExecutionPolicy Bypass -File C:\fn-monitor\windows\install-fn-scanner.ps1
   ```

3. Skripta vpraša za geslo. Windows ga shrani v opravilu. V skripto, v git in v dnevnik se ne zapiše.
4. Skripta registrira obe opravili in skener takoj zažene. Naslednji vklop strežnika ga zažene znova.

Ročni uvoz, če skripte ne zaženeš: v `windows\` sta `FN-Scanner.xml` in `FN-Scanner-Watchdog.xml`. V obeh zamenjaj `__INSTALL_DIR__` s `C:\fn-monitor` in `__RUN_AS__` z računom (na primer `IME-STREZNIKA\Administrator`). V Načrtovalniku opravil izberi **Import Task**, nato **Run whether user is logged on or not**, in vpiši geslo.

### Kaj teče

- **FN Scanner** zažene `windows\start-scanner.cmd`, ta pa `C:\fn-monitor\.venv\Scripts\python.exe scanner.py`. Zažene se ob vklopu, nima časovne omejitve in se po napaki znova zažene v eni minuti. Če ena instanca že teče, se druga ne zažene.
- **FN Scanner Watchdog** se zažene ob vklopu in nato vsakih 5 minut. Če procesa ni ali je `scanner_heartbeat.json` starejši od 12 minut, skener znova zažene. Onemogočenega opravila ne vklopi nazaj. Webhooka ne kliče.
- Dnevnik skenerja: `C:\fn-monitor\logs\scanner.out.log` (ko preseže 5 MB, se prepiše v `scanner.out.prev.log`).
- Srčni utrip, lokalna datoteka: `C:\fn-monitor\scanner_heartbeat.json`. Polji `utc` in `send_window_open` povesta, ali proces živi in ali je okno pošiljanja odprto. Novega webhooka za utrip ni.
- Nadzor zapiše `C:\fn-monitor\watchdog_heartbeat.json` ob vsakem preverjanju in `C:\fn-monitor\logs\watchdog.log` samo, ko skener znova zažene.

V Načrtovalniku je pri tekočem opravilu zadnji rezultat pogosto `267009`. To pomeni, da proces še teče, ne da je spodletel.

### Ustavi za vzdrževanje

V Načrtovalniku onemogoči **FN Scanner** in **FN Scanner Watchdog** (Disable). Samo End ni dovolj: ponovni zagon ob napaki in watchdog ju spet zaženeta. Za odstranitev obeh:

```
powershell -NoProfile -ExecutionPolicy Bypass -File C:\fn-monitor\windows\install-fn-scanner.ps1 -Uninstall
```

### Brez pošiljanja

`scanner.py --backfill` webhooka ne kliče. Za živi tek brez pošiljanja, na primer po prekopiranju datotek:

```
C:\fn-monitor\.venv\Scripts\python.exe scanner.py --no-send --once
```

`--once` ne prepiše `scanner_heartbeat.json`, zato watchdog zaradi kratkega preizkusa ne ustavi delujočega skenerja. Pravega webhooka testi ne kličejo.
