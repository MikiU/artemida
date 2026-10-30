# GSC Analyzer

Lokalna aplikacja w Pythonie do porównywania danych z **Google Search Console**.
Obsługuje **wiele serwisów** (konfiguracja w `sites.yaml`) i porównuje **dwa
wybrane serwisy** w tych samych dwóch okresach (CURRENT vs PREVIOUS). Przypisuje
URL-e do kategorii, buduje hierarchię, liczy pokrycie i eksportuje wyniki do CSV.

## Co robi

Po uruchomieniu `python app.py` program:

1. pokazuje listę serwisów z `sites.yaml` i pyta, które dwa porównać,
2. pyta raz o wspólne okresy (CURRENT i PREVIOUS),
3. dla każdego serwisu pobiera dane GSC (wymiar `page`) i sitemapy kategorii,
4. przypisuje URL-e do kategorii, rozpoznaje Homepage i Other,
5. buduje hierarchię kategorii i liczy classification/content coverage,
6. pokazuje `SITE COMPARISON`, różnicę dynamiki (pp), TOP-level kategorie każdego
   serwisu, porównanie `category_groups` (jeśli skonfigurowane), wykrycie
   przeciwnych kierunków oraz największe różnice,
7. zapisuje wyniki do katalogu `output/YYYY-MM-DD_HHMM_<a>_vs_<b>/`.

Cała analiza jest **deterministyczna** (Python/pandas) – bez AI/LLM.

---

## 1. Instalacja lokalna

W katalogu `gsc-analyzer/`:

```powershell
python -m venv .venv
```

Aktywacja środowiska:

- Windows (PowerShell):
  ```powershell
  .venv\Scripts\Activate.ps1
  ```
- Windows (cmd):
  ```cmd
  .venv\Scripts\activate.bat
  ```
- macOS / Linux:
  ```bash
  source .venv/bin/activate
  ```

Instalacja zależności:

```powershell
pip install -r requirements.txt
```

---

## 2. Konfiguracja Google Cloud + Search Console (krok po kroku)

Ta sekcja jest dla osoby, która nie zna dobrze Google Cloud. Wykonaj po kolei.

### 2.1. Utwórz projekt Google Cloud

1. Wejdź na https://console.cloud.google.com/
2. U góry kliknij listę projektów → **New Project**.
3. Nadaj nazwę (np. `gsc-analyzer`) i kliknij **Create**.
4. Upewnij się, że nowy projekt jest wybrany (nazwa u góry ekranu).

### 2.2. Włącz Search Console API

1. Menu → **APIs & Services** → **Library**.
2. Wyszukaj **Google Search Console API**.
3. Kliknij wynik i naciśnij **Enable**.

### 2.3. Utwórz Service Account (konto serwisowe)

1. Menu → **APIs & Services** → **Credentials**.
2. Kliknij **Create Credentials** → **Service account**.
3. Podaj nazwę (np. `gsc-reader`) i kliknij **Create and continue**.
4. Rolę możesz pominąć (kliknij **Continue**, potem **Done**).
   Dostęp do danych GSC nadamy osobno w Search Console.

### 2.4. Pobierz plik credentials (JSON)

1. Na liście **Credentials** kliknij utworzone konto serwisowe.
2. Zakładka **Keys** → **Add key** → **Create new key**.
3. Wybierz **JSON** → **Create**.
4. Plik pobierze się automatycznie. Zapisz go jako `credentials.json`.

> Adres e-mail konta serwisowego wygląda tak:
> `gsc-reader@twoj-projekt.iam.gserviceaccount.com`
> Skopiuj go – potrzebny w następnym kroku.

### 2.5. Dodaj konto serwisowe jako użytkownika property w Search Console

1. Wejdź na https://search.google.com/search-console
2. Wybierz swoją property (domenę).
3. **Settings** (Ustawienia) → **Users and permissions**.
4. **Add user**.
5. Wklej adres e-mail konta serwisowego (z kroku 2.4).
6. Uprawnienia: **Full** lub **Restricted** (do odczytu wystarczy Restricted).
7. Zapisz.

> Bez tego kroku API zwróci błąd 403 (brak dostępu do property).

### 2.6. Umieść credentials.json

Skopiuj pobrany plik do katalogu `gsc-analyzer/` (obok `app.py`) pod nazwą
`credentials.json`. Plik jest w `.gitignore` – nie trafi do repozytorium.

### 2.7. Skonfiguruj `.env`

Skopiuj `.env.example` do `.env`:

```powershell
copy .env.example .env
```

W `.env` trzymamy już **tylko sekret** (ścieżkę do klucza Service Account):

```
GOOGLE_CREDENTIALS_PATH=credentials.json
```

Konfiguracja serwisów (property, sitemapy, grupy kategorii) jest w `sites.yaml`.

### 2.8. Skonfiguruj `sites.yaml`

Każdy serwis ma unikalny klucz (np. `fakt`). Przykład:

```yaml
sites:

  fakt:
    name: "Fakt"
    gsc_property: "https://www.fakt.pl/"          # <-- TU wpisz property z GSC
    base_url: "https://www.fakt.pl/"
    category_sitemaps:
      - "https://www.fakt.pl/sitemap-categories.xml"   # <-- TU URL sitemapy kategorii

  onet_wiadomosci:
    name: "Onet Wiadomości"
    gsc_property: "https://wiadomosci.onet.pl/"   # <-- property Onetu
    base_url: "https://wiadomosci.onet.pl/"
    category_sitemaps:
      - "TUTAJ_WLASCIWY_URL_SITEMAPY"             # <-- URL sitemapy kategorii Onetu
```

- `gsc_property` – dokładnie jak w Search Console:
  - URL-prefix property: `https://wiadomosci.onet.pl/`
  - Domain property: `sc-domain:onet.pl`
- `brand_terms` (opcjonalne) – frazy brandowe liczone w raporcie Search trybu 4:
  dopasowywane jako fragmenty zapytania, bez względu na wielkość liter. Warto
  podać warianty z polskimi znakami i bez nich osobno. Przykład:

```yaml
    brand_terms:
      - "Onet"
      - "Onet wiadomości"
```

  GSC może ukrywać część zapytań i nie gwarantuje zwrócenia wszystkich wierszy,
  dlatego kliknięcia brandowe mogą nie sumować się do całkowitego ruchu Search.
- Konto serwisowe musi być dodane jako użytkownik **każdej** property (krok 2.5).

**Grupy kategorii (opcjonalnie)** – ręczne dopasowanie kategorii między
serwisami do porównania cross-site. Podając top-level node (np. `Wydarzenia`)
bierzesz całe poddrzewo – nie dodawaj osobno jego dzieci:

```yaml
category_groups:

  news:
    label: "Wiadomości"
    fakt:
      paths:
        - "Wydarzenia"
        - "Polityka"
    onet_wiadomosci:
      paths:
        - "Wiadomosci"

  money:
    label: "Pieniądze"
    fakt:
      paths:
        - "Pieniadze"
    onet_wiadomosci:
      paths:
        - "Biznes"
```

`paths` to etykiety z drzewa kategorii (kolumna `category_path`), np. `Wydarzenia`
albo `Wydarzenia > Polska`. Grupa jest porównywana tylko, gdy ma `paths` dla
**obu** wybranych serwisów.

---

## 3. Uruchomienie

```powershell
python app.py
```
Program najpierw pyta o **tryb**:

```
MODE
1. Porównaj DWA serwisy (cross-site + grupy kategorii)
2. Ranking wzrostów/spadków kategorii dla wielu serwisów (2+)
3. Search vs Discover + device + YMYL (wiele serwisów)
4. Jeden serwis po kategoriach: Search/Discover/News + presety dat
```

- **Tryb 1** – szczegółowe porównanie dwóch serwisów (SITE COMPARISON, różnica
  dynamiki, grupy kategorii, drill-down). Wybierasz dwa serwisy numerami.
- **Tryb 2** – wspólny ranking kategorii dla dowolnej liczby serwisów (2+),
  bez dopasowywania kategorii między serwisami. Wybierasz serwisy numerami po
  przecinku (np. `1,2,3`). Wynik to jedna lista `(serwis, kategoria)` ze
  wszystkich poziomów drzewa, posortowana po zmianie clicks (TOP rosnące i
  TOP spadające), plus `categories_all_sites.csv`.
- **Tryb 3** – rozdziela **Search (web)** i **Discover**, rozbija ruch po
  **device** (mobile/desktop/tablet) i dzieli kategorie na **YMYL vs non-YMYL**
  (na podstawie `ymyl_paths` z `sites.yaml`). Dla wybranych serwisów pokazuje:
  `SOURCE OVERVIEW`, `YMYL vs NON-YMYL`, `DEVICE BREAKDOWN` oraz TOP rosnące/
  spadające kategorie osobno dla Search i Discover. Eksport: `source_overview.csv`,
  `ymyl_summary.csv`, `device_breakdown.csv`, `categories_by_source.csv`.
- **Tryb 4** – analiza **jednego serwisu** po kategoriach z **presetami dat**
  (tydzień/2 tyg./miesiąc/YoY/miesiąc kalendarzowy/własne) i wyborem źródeł
  **Search / Discover / News**. Pokazuje, które kategorie urosły/spadły w danym
  okresie wraz z metrykami (clicks current/previous, zmiana, %, `number_of_urls`).
  Eksport per źródło: `<źródło>_category_tree.csv`, `<źródło>_pages.csv`, `summary.txt`.

Program pokaże listę serwisów i poprosi o wybór oraz o wspólne okresy:

```
AVAILABLE SITES
1. Fakt
2. Onet Wiadomości

First site: 1
Second site: 2

CURRENT PERIOD
  Start date (YYYY-MM-DD): 2026-07-01
  End date   (YYYY-MM-DD): 2026-07-31
PREVIOUS PERIOD
  Start date (YYYY-MM-DD): 2026-06-01
  End date   (YYYY-MM-DD): 2026-06-30
```

Wyniki trafiają do katalogu, np.:

```
output/2026-08-13_1520_fakt_vs_onet_wiadomosci/
    site_comparison.csv
    category_groups_comparison.csv        # jeśli skonfigurowano grupy
    summary.txt
    fakt/
        pages_comparison.csv
        categories_comparison.csv
        category_tree_comparison.csv
        other_urls.csv
    onet_wiadomosci/
        ... (te same 4 pliki)
```

> Dane GSC są cache'owane lokalnie w `.cache/gsc/` (klucz: property + daty +
> wymiary), więc ponowne porównanie tych samych okresów nie pobiera danych
> drugi raz. Cache nie miesza domen (property jest częścią klucza).

---

## 3b. Dashboard w przeglądarce (Streamlit)

Dashboard obsługuje jeden lub wiele wybranych serwisów w tym samym układzie:

```powershell
python -m streamlit run dashboard.py
```

Otworzy się w przeglądarce (np. `http://localhost:8501`). Dostępne są cztery widoki:

| Widok | Dane |
| --- | --- |
| Przegląd ruchu | Dzienne kliknięcia i wyświetlenia bez sitemap, zmiany i odchylenia. |
| Kategorie i treści | Rankingi wzrostów/spadków, pełne tabele kategorii i URL-i, YMYL, urządzenia Search oraz skonfigurowane grupy kategorii dla pary serwisów. |
| Branded search | Kliknięcia fraz `brand_terms` wybranych serwisów: zakres dat, porównanie okresów lub około 16 miesięcy dostępnej historii. Wyłącznie Search. |
| Lista URL-i | Wklejone lub wgrane adresy przypisywane do wybranych serwisów, metryki i dzienny wykres ruchu tylko wskazanych URL-i, ich udział w ruchu serwisów. |

W panelu po lewej wybierasz widok, serwisy, źródła i okres, następnie
**Uruchom analizę**. Pusty wybór nie oznacza wszystkich serwisów.
Wykres ma osobną linię dla każdego serwisu; opcjonalnie można dodać sumę
i poprzedni okres (linia przerywana, wyrównanie po numerze dnia, rzeczywiste
daty w dymku). Kolory serwisów są stałe między widokami. Zakładki **Łącznie**
i wybranych źródeł rozdzielają Search, Discover i News. Branded ma tylko Search.
Pełne tabele są widoczne od razu; ranking TOP 20 nie ogranicza pełnych wyników.

Daty ręczne pojawiają się tylko przy własnym zakresie, a daty porównawcze
tylko przy porównaniu. W każdym widoku można wyłączyć **Porównaj okresy**
i pobrać jeden zakres: ostatnie 7, 14 lub 30 dni, ostatni rok (365 dni),
ostatni pełny miesiąc, własne daty albo **Maksymalny zakres (16 miesięcy)**.
Maksymalna historia zaczyna się 16 miesięcy przed bieżącą datą i kończy
3 dni temu, zgodnie z przyjętym opóźnieniem GSC. Poszczególne properties
mogą mieć krótszą historię; ograniczenia kompletności raportów GSC nadal obowiązują.
Bez porównania kategorie są sortowane według ruchu, bez sztucznych zmian
względem zera i bez pobierania danych poprzedniego okresu.
Różne długości okresów są sygnalizowane i nie powodują
obcięcia dłuższego okresu. Cache jest w ustawieniach zaawansowanych.
Zmiana parametrów zapytania ukrywa nieaktualne wyniki; zmiana sumy, metryki,
progu odchylenia lub pobranie eksportu nie odpytuje ponownie GSC.

Przy włączonym porównaniu **Równe okresy (do 16 miesięcy)** domyślnie
dzieli dostępne okno historii na dwa sąsiadujące okresy o tej samej liczbie dni.
Jeśli liczba dni historii jest nieparzysta, pomija najstarszy dzień.
Można wybrać maksimum albo skrócić zakres suwakiem dni lub miesięcy.
Miesiące wyznaczają długość bieżącego zakresu, a poprzedni dostaje dokładnie
tyle samo dni, niezależnie od długości miesięcy. Oba zakresy i liczby dni
są widoczne w panelu bocznym. Poprzedni okres jest domyślnie nałożony
na bieżący na wykresie. Pełnych 16 miesięcy nie można porównać z poprzednimi
16 miesiącami, ponieważ GSC nie udostępnia tak długiej historii.

Adresy URL przypisywane są według hosta i ścieżki `base_url` oraz zakresu
property. Nieprzypisane i niejednoznaczne adresy blokują analizę do czasu
poprawienia listy lub wskazania właściciela. Lista URL-i jest pobierana
z filtrem strony po stronie GSC; filtr jest częścią klucza cache.
Pozycja jest dostępna tylko w zakładce Search, CTR wynika z sum kliknięć
i wyświetleń. ZIP zawiera pełne dane, statusy i parametry analizy; tabele
można również eksportować osobno do CSV.

Opcja **Eksport URL-i z datami i ruchem** w przeglądzie ruchu, kategoriach
i liście URL-i dodaje plik `url_daily.csv`, dostępny osobno i w ZIP-ie.
Każdy wiersz zawiera serwis, źródło, okres (`current`/`previous`), rzeczywistą
datę ruchu, URL, kliknięcia, wyświetlenia i CTR; pozycję tylko dla Search.
To data ruchu, nie publikacji artykułu. Lista URL-i ogranicza eksport do
wskazanych adresów, pozostałe widoki pobierają URL-e całego serwisu.
Eksport jest opcjonalny i pobiera dane dzień po dniu, korzystając z cache.
Długie okresy i duże serwisy mogą oznaczać wiele zapytań i bardzo duże pliki.
GSC zwraca najważniejsze wiersze, maksymalnie 50 000 dziennie na property
i źródło; plik może nie zawierać wszystkich URL-i, a dni bez zwróconych
wierszy nie są dopisywane jako zera. Błąd eksportu jednego serwisu/źródła
jest sygnalizowany i nie usuwa poprawnego raportu ani eksportów pozostałych.

**Ograniczenia danych:** suma obejmuje raportowany ruch, nie unikalnych
użytkowników. Nakładające się properties mogą zawyżać sumę (dashboard ostrzega).
Błąd pobrania jest oznaczany oddzielnie od poprawnie pobranego zera; częściowe
wyniki mają ostrzeżenie. Obowiązują limity i kompletność danych GSC; sumy
z wymiaru URL mogą różnić się od raportów dziennych. Brak `brand_terms`
lub konfiguracji YMYL jest jawnie sygnalizowany.

Testy (bez połączenia z GSC, z symulowanym API):

```powershell
python -m pytest tests -q
```

CLI (`python app.py`) i jego dotychczasowe tryby działają niezależnie.

---

## 3c. Publikacja jako jeden link (Streamlit Community Cloud)

Aby udostępnić dashboard pod jednym adresem, bez plików `.env`/`credentials.json`
na serwerze, użyj sekretów.

1. Wypchnij repo na GitHub (bez sekretów – patrz `.gitignore`).
2. Wejdź na https://share.streamlit.io → **New app** → wskaż repo i `dashboard.py`.
3. **Settings → Secrets** – wklej (wzór: `.streamlit/secrets.toml.example`):
   - `GOOGLE_CREDENTIALS_JSON` – pełna treść klucza Service Account (JSON),
   - `SITES_YAML` – zawartość Twojego `sites.yaml`,
   - `APP_PASSWORD` – hasło dostępu (zalecane przy publicznym linku).
4. Deploy. Aplikacja wczyta credentials i serwisy z sekretów.

> ⚠️ **Bezpieczeństwo:** hostowany dashboard odpytuje Twoje GSC. Każdy, kto ma
> link (i hasło), zobaczy dane skonfigurowanych domen. Ustaw `APP_PASSWORD`
> i/lub w Streamlit Cloud ustaw aplikację jako **prywatną** (allowlist e-mail).
> Konto serwisowe musi mieć dostęp w GSC do wszystkich property z `SITES_YAML`.

Lokalnie zamiast pliku możesz też użyć `.streamlit/secrets.toml` (gitignored).

---

## 4. Testy

```powershell
pip install -r requirements.txt
pytest
```

Testy nie łączą się z prawdziwym Google API – sprawdzają logikę kategorii
i porównania okresów.

---

## Struktura projektu

```
gsc-analyzer/
    app.py                  # CLI: wybór 2 serwisów + raport cross-site + eksport
    analysis.py             # analyze_site(): wspólny pipeline jednego serwisu
    config.py               # .env (sekrety) + sites.yaml (serwisy, grupy)
    sites.yaml              # konfiguracja serwisów i category_groups
    requirements.txt
    README.md
    .env.example
    .gitignore
    services/
        gsc_service.py      # pobieranie danych z GSC API (paginacja + cache)
        sitemap_service.py  # pobieranie i parsowanie sitemapy kategorii
    analytics/
        comparison.py       # compare_periods, drzewo, coverage, sum control
        categories.py       # przypisywanie kategorii, Homepage/Other
        category_groups.py  # agregacja grup, direction_pattern, growth diff
    tests/
        test_categories.py
        test_comparison.py
        test_tree.py
        test_sites.py
        test_category_groups.py
        test_analysis.py
    output/                 # wyniki: output/<data>_<a>_vs_<b>/
    .cache/gsc/             # lokalny cache danych GSC
```

## Zakres (Etap 2)

Porównanie dwóch serwisów jednocześnie, konfiguracja przez `sites.yaml`,
grupy kategorii cross-site. Bez OpenAI/GPT/LLM, bez FastAPI/Streamlit/GUI,
bez bazy danych. Analiza pojedynczego serwisu (Etap 1/1.5) pozostaje
matematycznie niezmieniona (`analyze_site` reużywa te same funkcje).

