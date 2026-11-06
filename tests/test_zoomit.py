import httpx
import pytest
from conftest import fixture

pytestmark = pytest.mark.anyio

TOOLS = {
    "zm_categories",
    "zm_search",
    "zm_list",
    "zm_filters",
    "zm_product",
    "zm_prices",
    "zm_compare",
    "zm_similar",
    "zm_articles",
    "zm_reviews",
    "zm_guides",
    "zm_rank",
}
P = "/catalog/api/products/"


async def test_all_tools_are_read_only_and_described(client):
    tools = (await client.list_tools()).tools
    assert {t.name for t in tools} == TOOLS
    for t in tools:
        assert t.annotations.read_only_hint is True, t.name
        assert t.title and t.description.splitlines()[0].endswith("."), t.name
        assert "title" not in str(t.input_schema.get("properties", {}).values()), t.name


async def test_categories_filter_by_name(client, api):
    api["/catalog/api/categories"] = fixture("categories.json")
    r = (await client.call_tool("zm_categories", {"query": "mobile"})).structured_content
    assert r["count"] == 1 and r["categories"][0]["slug"] == "mobile"


async def test_search_returns_cards_in_toman(client, api):
    api[P + "search"] = fixture("search.json")
    r = (await client.call_tool("zm_search", {"query": "iphone", "limit": 3})).structured_content
    first = r["products"][0]
    assert r["total"] > 3 and first["slug"] and first["price_from"] > 1_000_000
    assert first["image"].startswith("https://api2.zoomit.ir/media/") and "base64" not in first["image"]
    q = api.calls[0].url.params
    assert q["query"] == "iphone" and q["pageSize"] == "3" and "categorySlug" not in q


async def test_list_resolves_brand_spec_and_sends_filters(client, api):
    api["/catalog/api/categories/mobile"] = fixture("category.json")
    api[P + "search"] = fixture("search.json")
    args = {
        "category": "mobile",
        "brands": ["apple"],
        "specs": ["5G=دارد"],
        "sort": "cheapest",
        "max_price": 90_000_000,
        "min_score": 4,
        "in_stock_only": True,
    }
    await client.call_tool("zm_list", args)
    q = api.calls[-1].url.params
    assert q["categorySlug"] == "mobile" and q["brands"] == "apple" and q["specification_12511"] == "Have"
    assert q["sort"] == "Cheapest" and q["minPrice"] == "1" and q["maxPrice"] == "90000000"
    assert q["minScore"] == "4" and q["onlyInStock"] == "true" and q["pageNumber"] == "1"


async def test_list_rejects_unknown_brand_and_spec(client, api):
    api["/catalog/api/categories/mobile"] = fixture("category.json")
    r = await client.call_tool("zm_list", {"category": "mobile", "brands": ["nokiaa"]})
    assert r.is_error and "zm_filters" in r.content[0].text
    r = await client.call_tool("zm_list", {"category": "mobile", "specs": ["رم=7 گیگابایت رم"]})
    assert r.is_error and "Options" in r.content[0].text


async def test_list_turns_upstream_500_on_empty_filters_into_no_results(client, api):
    api["/catalog/api/categories/mobile"] = fixture("category.json")
    api[P + "search"] = lambda request: httpx.Response(500, json={})
    r = (
        await client.call_tool("zm_list", {"category": "mobile", "specs": ["5G=دارد"], "max_price": 1000})
    ).structured_content
    assert r["total"] == 0 and r["products"] == []


async def test_filters_lists_brands_specs_and_guides(client, api):
    api["/catalog/api/categories/mobile"] = fixture("category.json")
    r = (await client.call_tool("zm_filters", {"category": "mobile"})).structured_content
    assert r["brands"][0] == {"slug": "samsung", "title": "سامسونگ"} and len(r["brands"]) == 6
    assert {s["id"] for s in r["specs"]} == {12459, 12511} and r["buying_guides"]
    one = (await client.call_tool("zm_filters", {"category": "mobile", "spec": "5G"})).structured_content
    assert one["specs"][0]["values"] == ["دارد", "ندارد"]


async def test_product_has_spec_table_pros_and_price(client, api):
    api[P + "apple-iphone-17-pro"] = fixture("product.json")
    r = (
        await client.call_tool("zm_product", {"slug": "https://www.zoomit.ir/product/apple-iphone-17-pro/"})
    ).structured_content
    assert r["slug"] == "apple-iphone-17-pro" and r["brand"] and r["category"] == "mobile"
    assert r["price_from"] < r["price_to"] and r["pros"] and r["cons"] and r["user_score"]
    assert r["specs"] and all(isinstance(v, str) for sec in r["specs"].values() for v in sec.values())
    assert "\n" not in str(r["specs"])


async def test_product_unknown_section_and_bad_slug(client, api):
    api[P + "apple-iphone-17-pro"] = fixture("product.json")
    r = await client.call_tool("zm_product", {"slug": "apple-iphone-17-pro", "sections": ["zzz"]})
    assert r.is_error and "Sections" in r.content[0].text
    r = await client.call_tool("zm_product", {"slug": "not a slug!"})
    assert r.is_error


async def test_prices_sorted_with_history(client, api):
    prod = fixture("product.json")
    pid = prod["product"]["id"]
    api[P + "apple-iphone-17-pro"] = prod
    api[P + f"{pid}/price-chart"] = fixture("chart.json")
    r = (
        await client.call_tool("zm_prices", {"slug": "apple-iphone-17-pro", "history": "year", "limit": 3})
    ).structured_content
    prices = [o["price"] for o in r["offers"]]
    assert prices == sorted(prices)
    h = r["history"]
    assert h["lowest"]["price"] <= h["last"]["price"] <= h["highest"]["price"] and h["sample"]
    assert api.calls[-1].url.params["timeRange"] == "LastYear"


async def test_prices_without_history_skips_chart(client, api):
    api[P + "apple-iphone-17-pro"] = fixture("product.json")
    r = (await client.call_tool("zm_prices", {"slug": "apple-iphone-17-pro", "history": "none"})).structured_content
    assert "history" not in r and not any("price-chart" in str(c.url) for c in api.calls)


async def test_compare_aligns_values_and_hides_equal_rows(client, api):
    api[P + "apple-iphone-17-pro"] = fixture("product.json")
    api[P + "samsung-galaxy-s26-ultra"] = fixture("product2.json")
    r = (
        await client.call_tool("zm_compare", {"slugs": ["apple-iphone-17-pro", "samsung-galaxy-s26-ultra"]})
    ).structured_content
    assert [p["slug"] for p in r["products"]] == ["apple-iphone-17-pro", "samsung-galaxy-s26-ultra"]
    rows = [row for sec in r["specs"].values() for row in sec]
    assert rows and all(len(row) == 3 and row[1] != row[2] for row in rows)


async def test_compare_needs_two_products(client, api):
    r = await client.call_tool("zm_compare", {"slugs": ["apple-iphone-17-pro"]})
    assert r.is_error


async def test_similar_calls_the_matching_endpoint(client, api):
    prod = fixture("product.json")
    pid = prod["product"]["id"]
    api[P + "apple-iphone-17-pro"] = prod
    api[f"/catalog/api/comparisons/related/{pid}"] = fixture("related.json")
    api[P + f"{pid}/related/price"] = fixture("related.json")
    r = (await client.call_tool("zm_similar", {"slug": "apple-iphone-17-pro", "limit": 2})).structured_content
    assert r["kind"] == "compared" and len(r["products"]) == 2
    await client.call_tool("zm_similar", {"slug": "apple-iphone-17-pro", "kind": "price"})
    q = api.calls[-1].url.params
    assert int(q["MinPrice"]) < int(q["MaxPrice"]) and q["CategoryId"] == "1"


async def test_articles_get_site_urls(client, api):
    api["/editorial/api/search/v2"] = fixture("articles.json")
    r = (await client.call_tool("zm_articles", {"query": "iphone", "limit": 2})).structured_content
    assert len(r["articles"]) == 2 and r["articles"][0]["url"].startswith("https://www.zoomit.ir/")
    assert api.calls[0].url.params["Query"] == "iphone"


async def test_blocked_request_explains_itself(client, api):
    api["/catalog/api/categories"] = lambda request: httpx.Response(403, text="blocked")
    r = await client.call_tool("zm_categories", {})
    assert r.is_error and "403" in r.content[0].text


async def test_reviews_page_one_has_totals_summary_and_trimmed_items(client, api):
    prod = fixture("product.json")
    api[P + "apple-iphone-17-pro"] = prod
    api["/discussion/api/feedbacks"] = fixture("feedbacks_review.json")
    api["/discussion/api/review-info/Product/146185"] = fixture("review_info.json")
    api["/discussion/api/discussion-info/Product/146185"] = fixture("discussion_info.json")
    api["/discussion/api/topic-feedback-summaries"] = fixture("summary.json")
    r = (
        await client.call_tool("zm_reviews", {"slug": "apple-iphone-17-pro", "stars": 5, "sort": "newest", "limit": 3})
    ).structured_content
    q = api.calls[1].url.params
    assert q["type"] == "Review" and q["sortBy"] == "Latest" and q["authorScore"] == "5" and q["offset"] == "1"
    assert r["stars_breakdown"]["5"] > 0 and r["totals"]["reviews"] and r["summary"]["strengths"]
    first = r["items"][0]
    assert first["type"] == "review" and first["stars"] and first["text"] and "userId" not in first
    assert any(i.get("replies") for i in r["items"])


async def test_reviews_questions_later_page_skips_extras(client, api):
    api[P + "apple-iphone-17-pro"] = fixture("product.json")
    api["/discussion/api/feedbacks"] = fixture("feedbacks_question.json")
    r = (
        await client.call_tool("zm_reviews", {"slug": "apple-iphone-17-pro", "kind": "question", "page": 2})
    ).structured_content
    assert api.calls[-1].url.params["type"] == "Question" and api.calls[-1].url.params["offset"] == "2"
    assert "summary" not in r and r["items"][0]["type"] == "question"


async def test_guides_lists_articles_and_price_band(client, api):
    api["/catalog/api/buying-guides"] = fixture("guides.json")
    r = (await client.call_tool("zm_guides", {"category": "mobile", "page": 1})).structured_content
    g = r["guides"][0]
    assert r["total"] > 8 and g["title"] and g["price_from"] < g["price_to"] and g["article"].startswith("https://")


async def test_rank_orders_by_numeric_spec_and_by_value(client, api):
    cmp = fixture("comparison.json")
    slugs = [p["slug"] for p in cmp["products"]]
    search = fixture("search.json")
    search["products"]["source"] = search["products"]["source"][:2]
    for card, slug in zip(search["products"]["source"], slugs, strict=True):
        card["slug"], card["minPrice"] = slug, 1_000_000_000 if slug == slugs[1] else 500_000_000
    api[P + "search"] = search
    api["/catalog/api/comparisons/mobile"] = cmp
    r = (await client.call_tool("zm_rank", {"category": "mobile", "by": "ظرفیت باتری"})).structured_content
    metrics = [p["metric"] for p in r["products"]]
    assert r["ranked_by"] == "ظرفیت باتری" and metrics == sorted(metrics, reverse=True) and metrics[0] > 1000
    assert api.calls[-1].url.params["slugs[1]"] == slugs[1]
    r = (await client.call_tool("zm_rank", {"category": "mobile", "by": "value", "order": "low"})).structured_content
    assert r["ranked_by"].startswith("user score") and len(api.calls) == 2
    bad = await client.call_tool("zm_rank", {"category": "mobile", "by": "zzz"})
    assert bad.is_error and "Candidates" in bad.content[0].text


async def test_prices_registered_only_drops_unregistered(client, api):
    prod = fixture("product.json")
    api[P + "apple-iphone-17-pro"] = prod
    stores = prod["product"]["stores"]
    stores["items"][0]["purchaseItems"][0]["registry"] = False
    r = (
        await client.call_tool("zm_prices", {"slug": "apple-iphone-17-pro", "history": "none", "registered_only": True})
    ).structured_content
    assert all(o["registered"] is not False for o in r["offers"])


async def test_request_headers_match_the_sites_frontend(api):
    from zoomit_mcp import http

    api["/catalog/api/categories"] = {}
    await http.fetch(f"{http.CATALOG}/categories")
    h = api.calls[-1].headers
    assert h["accept"] == "application/json, text/plain, */*"
    assert h["origin"] == "https://www.zoomit.ir" and h["referer"] == "https://www.zoomit.ir/"
    assert (h["sec-fetch-site"], h["sec-fetch-mode"], h["sec-fetch-dest"]) == ("same-site", "cors", "empty")
    assert h["priority"] == "u=1, i" and h["accept-language"].startswith("fa-IR")
