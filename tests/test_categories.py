"""Testy przypisywania URL-i do kategorii."""
from analytics.categories import OTHER, assign_categories, match_category, build_category_index

CATEGORY_URLS = [
    "https://example.com/sport/",
    "https://example.com/sport/pilka-nozna/",
    "https://example.com/sport/tenis/",
    "https://example.com/wiadomosci/",
    "https://example.com/wiadomosci/polska/",
]


def test_matches_correct_category():
    index = build_category_index(CATEGORY_URLS)
    label = match_category("https://example.com/wiadomosci/polska/wybory.html", index)
    assert label == "Wiadomosci > Polska"


def test_chooses_longest_matching_category():
    index = build_category_index(CATEGORY_URLS)
    url = "https://example.com/sport/pilka-nozna/lewandowski-strzelil-gola.html"
    assert match_category(url, index) == "Sport > Pilka Nozna"


def test_url_without_category_goes_to_other():
    index = build_category_index(CATEGORY_URLS)
    assert match_category("https://example.com/gielda/notowania.html", index) == OTHER


def test_segment_boundary_is_respected():
    # /sporty/ nie powinno pasować do kategorii /sport/
    index = build_category_index(CATEGORY_URLS)
    assert match_category("https://example.com/sporty/cos.html", index) == OTHER


def test_assign_categories_adds_column():
    import pandas as pd

    df = pd.DataFrame(
        {
            "page": [
                "https://example.com/sport/tenis/mecz.html",
                "https://example.com/inne/artykul.html",
            ]
        }
    )
    result = assign_categories(df, CATEGORY_URLS)
    assert list(result["category_path"]) == ["Sport > Tenis", OTHER]
