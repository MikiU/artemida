# GSC Analyzer (Etap 2 – porównanie dwóch serwisów)

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

Zamiast CLI możesz użyć graficznego dashboardu (te same analizy, klikalny
interfejs: wybór serwisów, kalendarz dat, sortowalne tabele, wykresy, pobieranie
CSV):

```powershell
streamlit run dashboard.py
```

Otworzy się w przeglądarce (np. `http://localhost:8501`). W panelu po lewej
wybierasz tryb (1/2/3), serwisy, okresy i klikasz **Uruchom analizę**.
CLI (`python app.py`) działa dalej niezależnie.

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

