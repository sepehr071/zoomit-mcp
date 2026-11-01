import pytest

pytestmark = [pytest.mark.live, pytest.mark.anyio]


async def test_live_categories(client):
    r = (await client.call_tool("zm_categories", {})).structured_content
    assert r["count"] > 50 and {"mobile", "laptop", "vga"} <= {c["slug"] for c in r["categories"]}


async def test_live_search_and_product(client):
    r = (await client.call_tool("zm_search", {"query": "iphone 17 pro", "limit": 5})).structured_content
    slugs = [p["slug"] for p in r["products"]]
    assert "apple-iphone-17-pro" in slugs, slugs
    p = (await client.call_tool("zm_product", {"slug": "apple-iphone-17-pro"})).structured_content
    assert p["brand"] and p["specs"] and p["price_from"] > 1_000_000


async def test_live_list_with_filters(client):
    r = (
        await client.call_tool(
            "zm_list",
            {"category": "mobile", "brands": ["apple"], "specs": ["5G=دارد"], "sort": "cheapest", "limit": 5},
        )
    ).structured_content
    prices = [p["price_from"] for p in r["products"]]
    assert r["total"] > 5 and prices == sorted(prices) and all(p["brand"] for p in r["products"])


async def test_live_prices_and_history(client):
    r = (await client.call_tool("zm_prices", {"slug": "apple-iphone-17-pro", "limit": 5})).structured_content
    assert r["offers"] and r["history"]["points"] > 5


async def test_live_compare_and_similar(client):
    r = (
        await client.call_tool("zm_compare", {"slugs": ["apple-iphone-17-pro", "samsung-galaxy-s26-ultra"]})
    ).structured_content
    assert len(r["products"]) == 2 and r["specs"]
    s = (await client.call_tool("zm_similar", {"slug": "apple-iphone-17-pro", "limit": 3})).structured_content
    assert s["products"]


async def test_live_filters_and_articles(client):
    f = (await client.call_tool("zm_filters", {"category": "laptop"})).structured_content
    assert f["brands"] and f["specs"]
    a = (await client.call_tool("zm_articles", {"query": "آیفون", "limit": 3})).structured_content
    assert a["articles"]


async def test_live_reviews_guides_rank(client):
    r = (await client.call_tool("zm_reviews", {"slug": "apple-iphone-17-pro", "limit": 3})).structured_content
    assert r["totals"]["reviews"] > 10 and r["items"] and r["stars_breakdown"]
    g = (await client.call_tool("zm_guides", {"category": "mobile"})).structured_content
    assert g["total"] > 8 and g["guides"][0]["products"]
    k = (
        await client.call_tool("zm_rank", {"category": "mobile", "by": "ظرفیت باتری", "candidates": 8, "limit": 3})
    ).structured_content
    m = [p["metric"] for p in k["products"]]
    assert m and m == sorted(m, reverse=True)
