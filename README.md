# fn-monitor

Monitor čakajočih nalogov in skener priložnosti, ki tečeta na GitHub Actions. Nadomestita procesa, ki sta se na strežniku zmrzovala.

## Kaj počne

- **Monitor** (`monitor.py`) vsakih 60 sekund preveri čakajoče naloge in pošlje webhook ob dogodkih `near_entry`, `triggered`, `m15_close_against`, `near_sl`, `sl_hit`, `tp_hit` in `halfway_to_tp`.
- **Skener** (`scanner.py`) pregleda deset simbolov (EURUSD, GBPUSD, AUDUSD, USDJPY, XAUUSD, USOUSD, BTCUSD, NAS100, GER40, US30) in javi kandidate `breakout`, `retest`, `pullback` in `exhaustion`. Zaključen M15 breakout ne čaka na M1. Zgodnji vstop okoli odprtja je dodatek in ne zavrne poznejšega M15. USOUSD je surova nafta WTI. Zunaj odprtij sej teče na 5 minut; v oknih odprtja (08:45–10:00 in 15:15–16:30 po času Europe/Ljubljana) na približno 60 sekund.
- Oba tečeta v istem zagonu približno 28 minut. Razpored je na vsakih 15 minut ob delavnikih, od 06:00 do 20:45 UTC (pozimi približno 07:00–21:45 po času Europe/Ljubljana, poleti uro pozneje). Tek, ki že teče, se ne prekine. Če GitHub zamudi, skener ob naslednjem zagonu še pošlje sveče, ki so se zaprle v zadnjih 45 minutah; starejše samo zapiše kot `zamujen`.
- Stanje (že poslani dogodki, cooldowni, dnevne omejitve) se med zagoni shrani v predpomnilnik Actions, da se isto opozorilo ne pošlje dvakrat. Skener sme na dan poslati največ **2 kandidata na simbol** in **8 skupaj**. Med tipom in simbolom velja še **90-minutni** cooldown.
- BTCUSD je samo v skenerju. Med nalogi ga ni, ker ga je posrednik zavrnil.

## Kako skener sestavi signal

Petdnevni capped replay (5., 6., 9., 10. in 11. november 2026, cilj 2R) je osnovno pravilo M15 ponovil pri **+4,71R**. Trda zahteva M1+M5+M15 je padla na +2,34R in je izgubila 11 od 13 breakout poslov. Hibrid v tej veji je bil +1,71R, torej slabši. Koda je za pregled, ne kot zamenjava osnovnega pravila.

- **H1** ostane filter trenda (EMA 9/20). Breakout RVOL ostane **≥ 1.5**. Odmik 0,15× H1 ATR za FX, GER40 in WTI ostane. Novice ±30 minut, cap in 90-minutni cooldown ostanejo. Exhaustion proti jasnemu H1 trendu odpade.
- **Zaključen M15 breakout**, ki gre skozi H1, RVOL in odmik, je veljaven. Manjkajoč M1 ali M5 ga ne zavrže. Če je M5 na voljo, velja stari test (zaprtje čez sredino M15 in EMA 9/20). Padec testa zavrže breakout. Uspeh da M5 stop, kot doslej. M1 se zapiše in ni veto.
- **Retest, pullback in exhaustion** čakajo na zaključeno M15 in rabijo isti M5 test. Brez M5 jih ni. Osamljen M1 skok ni vstop.
- Breakout, ki ostane, je **A+**. Retest, pullback in exhaustion so **B+** samo z M5 potrditvijo.

### Odprtja sej

Okoli londonskega odprtja in ameriškega denarnega odprtja skener ne čaka na zaključek M15. Okna so v času **Europe/Ljubljana**, konec ni vključen. 09:00 po ljubljansko je londonsko odprtje vse leto, ker oba premakneta uro skupaj. 15:30 je običajno NYSE 09:30; v tednu ali dveh, ko se poletni čas v ZDA in Evropi ne preklopi isti vikend, je okno dovolj široko, da odprtje še vedno pade vanj.

- **london** 08:45–10:00
- **us_cash** 15:15–16:30

V oknu zanka spi `boost_interval_seconds` (privzeto 60) in bere zaključene M1 in M5. Breakout pred zaključkom M15 je dovoljen samo, če se tekoča M15, zaključena M5 (RVOL ≥ 1.5, isti odmik) in M1 timing ujemajo. Ta zgodnji alarm ima svoj cooldown in ne blokira poznejšega zaključenega M15 breakouta. Zaprtje točno ob :00, :15, :30 ali :45 prevzame zaključena M15. Retest, pullback in exhaustion čakajo na zaključeno svečo.

Zunaj oken skener ostane na 5 minutah in odloča ob zaključeni M15. Sveče, starejše od 45 minut, zapiše kot zamujene in jih ne pošlje. `--backfill` in `--no-send` webhooka ne kličeta. Testi ga tudi ne smejo.

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

Skener lahko teče pri tebi, neprekinjeno, namesto na GitHub Actions. Program sam pošilja samo ob delavnikih od 08:00 do 21:00 po času Europe/Ljubljana, zato je opravilo lahko zagnano ob prijavi in lahko ostane odprto.

1. Namesti Python 3.12 in odpri mapo repozitorija v ukazni vrstici.
2. Ustvari okolje in namesti odvisnosti. `tzdata` je v `requirements.txt` in je na Windowsu potreben za časovni pas `Europe/Ljubljana`:

   ```
   py -3.12 -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt
   ```

3. Naslov in ključ nastavi kot uporabniški spremenljivki okolja, ne v `config.json` in ne v git:
   - `FN_WEBHOOK_URL` je polni URL webhooka
   - `FN_WEBHOOK_KEY` je ključ, brez besede Bearer
4. Odpri **Task Scheduler** → **Create Task**.
   - Trigger: **At log on**
   - Action: **Start a program**
   - Program: polna pot do `.venv\Scripts\python.exe`
   - Arguments: `scanner.py`
   - Start in: mapa repozitorija
5. V zavihku **Conditions** izklopi zagon samo ob napajanju iz elektrike, če je računalnik prenosnik. Opravilo naj teče z uporabnikom, ki ima nastavljeni spremenljivki, in samo kadar je ta uporabnik prijavljen, da ju Python vidi.
6. Okno oddaljenega namizja zapri z **Disconnect** (križec na vrhu), ne z **Sign out**. Odjava ustavi opravilo ob prijavi, prekinitev povezave pa ga pusti teči.
