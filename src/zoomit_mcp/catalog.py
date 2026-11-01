"""Zoomit product database tools (Zoobin): categories, search, filtered lists, specs, prices, comparisons."""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta
from typing import Annotated, Any, Literal

from pydantic import Field

from .http import API, CATALOG, EDITORIAL, SITE, ApiError, fetch
from .registry import tool

Slug = Annotated[
    str,
    Field(
        description="Product slug from zm_search or zm_list, e.g. apple-iphone-17-pro (a zoomit.ir/product/... URL also works).",
        min_length=2,
        max_length=200,
    ),
]
Category = Annotated[
    str,
    Field(
        description="Category slug from zm_categories, e.g. mobile, laptop, vga, tv, monitor.",
        pattern=r"^[a-z0-9-]{2,60}$",
    ),
]
Limit = Annotated[int, Field(description="How many results to return.", ge=1, le=40)]

SORTS = {
    "popular": "MostPopular",
    "cheapest": "Cheapest",
    "expensive": "MostExpensive",
    "newest": "Newest",
    "user_score": "HighestUserScore",
    "zoomit_score": "HighestZoomitScore",
    "discussed": "MostDiscussed",
}
RANGES = {
    "month": "LastMonth",
    "3months": "LastThreeMonths",
    "6months": "LastSixMonths",
    "year": "LastYear",
    "all": None,
}
TEHRAN_UTC = timedelta(hours=3, minutes=30)  # chart ticks are Tehran midnight in UTC


def _slug(value: str) -> str:
    """Accept a bare slug or a zoomit.ir/product/<slug>/ URL."""
    m = re.search(r"/product/(?:[a-z-]+/)?([a-z0-9][a-z0-9-]*)/?(?:[?#].*)?$", value)
    s = (m.group(1) if m else value).strip().strip("/")
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]*", s):
        raise ApiError(f"'{value}' is not a product slug. Use the slug from zm_search or zm_list.")
    return s


def _norm(text: str) -> str:
    return re.sub(r"[\s\u200c]+", "", text).lower()


def _img(image: dict | None) -> str | None:
    return f"{API}/media/{image['fileName']}-{image['id']}?w=640&q=75" if image and image.get("id") else None


def _price(v: float | None) -> int | None:
    """Zoomit prices are Toman; 0 means no price listed."""
    return int(v) if v else None


def _specs_line(p: dict) -> list[str]:
    out = []
    for k in p.get("keySpecifications") or []:
        a, b = (k.get("primaryValue") or "").strip(), (k.get("secondaryValue") or "").strip()
        if a:
            out.append(f"{a} ({b})" if b else a)
    return out


def _card(p: dict) -> dict:
    return {
        "slug": p["slug"],
        "title": p["title"],
        "english": p.get("englishTitle"),
        "brand": (p.get("brand") or {}).get("title"),
        "category": (p.get("category") or {}).get("slug"),
        "price_from": _price(p.get("minPrice")),
        "in_stock": p.get("inStock"),
        "score": p.get("score") or None,
        "sellers": p.get("sellerCount"),
        "key_specs": _specs_line(p),
        "image": _img(p.get("image")),
    }


def _page(data: dict, page_size: int) -> dict:
    p = data["products"]
    return {
        "total": p["totalCount"],
        "page": p["currentPage"],
        "pages": p["totalPages"],
        "page_size": page_size,
        "products": [_card(x) for x in p["source"]],
    }


async def _category(slug: str) -> dict:
    return (await fetch(f"{CATALOG}/categories/{slug}"))["category"]


async def _product(slug: str) -> dict:
    return (await fetch(f"{CATALOG}/products/{_slug(slug)}"))["product"]


@tool("List product categories")
async def zm_categories(
    query: Annotated[
        str | None, Field(description="Part of a Persian name or slug to filter, e.g. لپ تاپ or tv.")
    ] = None,
) -> dict:
    """List the product categories of Zoomit's product database (slug and Persian name).

    Use the slug as `category` in zm_list and zm_filters; zm_search can also be limited to one.
    """
    cats = await fetch(f"{CATALOG}/categories")
    q = _norm(query or "")
    rows = [{"slug": c["slug"], "title": c["title"]} for c in cats if q in _norm(c["title"] + c["slug"])]
    return {"count": len(rows), "categories": rows}


@tool("Search products by name")
async def zm_search(
    query: Annotated[
        str,
        Field(
            description="Product name or model, Persian or English, e.g. iphone 17 pro, گلکسی s26, rtx 5070.",
            min_length=1,
            max_length=100,
        ),
    ],
    category: Annotated[
        str | None, Field(description="Limit to one category slug from zm_categories.", pattern=r"^[a-z0-9-]{2,60}$")
    ] = None,
    limit: Limit = 10,
) -> dict:
    """Find products by name across all categories (or one) and return cards with the lowest price in Toman.

    Use when the user names a product. Next: zm_product for specs, zm_prices for sellers, zm_compare for versus.
    """
    params: dict[str, Any] = {"query": query, "pageSize": limit}
    if category:
        params["categorySlug"] = category
    return _page(await fetch(f"{CATALOG}/products/search", params), limit)


async def _resolve(cat: dict, brands: list[str] | None, specs: list[str] | None, guide: str | None) -> dict:
    """Turn friendly brand/spec/guide names into the ids the list API expects."""
    out: dict[str, Any] = {}
    if brands:
        known = {_norm(b["value"]): b["value"] for b in cat["brands"]} | {
            _norm(b["label"]): b["value"] for b in cat["brands"]
        }
        slugs = []
        for b in brands:
            if _norm(b) not in known:
                raise ApiError(
                    f"Unknown brand '{b}' for {cat['slug']}. Call zm_filters(category='{cat['slug']}') for the brand slugs."
                )
            slugs.append(known[_norm(b)])
        out["brands"] = ",".join(slugs)
    picked: dict[int, list[str]] = {}
    for item in specs or []:
        key, _, val = item.partition("=")
        if not val:
            raise ApiError(
                f"Spec filter '{item}' must look like 'name=value', e.g. 'رم=8 گیگابایت رم'. See zm_filters."
            )
        cands = [s for s in cat["specifications"] if key.strip() == str(s["id"]) or _norm(key) in _norm(s["title"])]
        if len(cands) != 1:
            names = ", ".join(f"{s['id']}:{s['title']}" for s in (cands or cat["specifications"])[:10])
            raise ApiError(f"Spec '{key}' matches {len(cands)} filters of {cat['slug']}. Candidates: {names}")
        spec = cands[0]
        vals = spec.get("values") or []
        hit = [v for v in vals if _norm(v["label"]) == _norm(val)] or [v for v in vals if v["value"] == val.strip()]
        hit = hit or [v for v in vals if _norm(val) in _norm(v["label"])]
        if len(hit) != 1:
            names = ", ".join(f"{v['value']}:{v['label']}" for v in (hit or vals)[:12])
            raise ApiError(f"Value '{val}' matches {len(hit)} options of '{spec['title']}'. Options: {names}")
        picked.setdefault(spec["id"], []).append(hit[0]["value"])
    for sid, vs in picked.items():
        out[f"specification_{sid}"] = ",".join(sorted(vs))
    if guide:
        g = [x for x in cat["buyingGuides"] if _norm(guide) in _norm(x["title"]) or guide == x["id"]]
        g = [x for x in g if _norm(guide) == _norm(x["title"])] or g
        if len(g) != 1:
            names = " | ".join(x["title"] for x in (g or cat["buyingGuides"])[:8])
            raise ApiError(f"Buying guide '{guide}' matches {len(g)}; use more of the title. Options: {names}")
        out["buyingGuideIds"] = g[0]["id"]
    return out


@tool("List products with filters")
async def zm_list(
    category: Category,
    brands: Annotated[
        list[str] | None, Field(description="Brand slugs or names from zm_filters, e.g. ['apple','samsung'].")
    ] = None,
    min_price: Annotated[int | None, Field(description="Lowest price in Toman.", ge=0)] = None,
    max_price: Annotated[int | None, Field(description="Highest price in Toman.", ge=0)] = None,
    min_score: Annotated[int | None, Field(description="Minimum user score, whole stars.", ge=1, le=5)] = None,
    sort: Annotated[
        Literal["popular", "cheapest", "expensive", "newest", "user_score", "zoomit_score", "discussed"],
        Field(description="Order of results."),
    ] = "popular",
    in_stock_only: Annotated[bool, Field(description="Only products with a seller in stock.")] = False,
    priced_only: Annotated[
        bool, Field(description="Hide products with no listed price. Always on for sort=cheapest.")
    ] = False,
    specs: Annotated[
        list[str] | None,
        Field(
            description="Spec filters 'name=value' from zm_filters, e.g. ['رم=8 گیگابایت رم', 'پشتیبانی از 5G=دارد']. Same name twice means either value."
        ),
    ] = None,
    buying_guide: Annotated[
        str | None,
        Field(description="Title (or part) of a buying guide from zm_filters; keeps only the products it recommends."),
    ] = None,
    query: Annotated[str | None, Field(description="Text to search within the category.")] = None,
    page: Annotated[int, Field(description="Page number, 1-based.", ge=1)] = 1,
    limit: Limit = 20,
) -> dict:
    """List one category's products with the site's filters: brand, price, score, specs, stock, sort.

    Use for "best/cheapest X under N Toman". Prices are Toman. Get brand slugs and spec filter values from
    zm_filters first. Next: zm_product for one result, zm_compare for several.
    """
    params: dict[str, Any] = {"categorySlug": category, "pageSize": limit, "pageNumber": page}
    if specs or brands or buying_guide:
        params |= await _resolve(await _category(category), brands, specs, buying_guide)
    if sort != "popular":
        params["sort"] = SORTS[sort]
    lo = min_price or (1 if priced_only or sort == "cheapest" else None)
    if lo:
        params["minPrice"] = lo
    if max_price:
        params["maxPrice"] = max_price
    if min_score:
        params["minScore"] = min_score
    if in_stock_only:
        params["onlyInStock"] = "true"
    if query:
        params["query"] = query
    try:
        return _page(await fetch(f"{CATALOG}/products/search", params), limit)
    except ApiError as e:
        if e.status == 500 and specs and (min_price or max_price):
            # The site's API answers HTTP 500 instead of an empty list when spec + price filters match nothing.
            return {
                "total": 0,
                "page": page,
                "pages": 0,
                "page_size": limit,
                "products": [],
                "note": "Nothing matches these filters.",
            }
        raise


@tool("Show a category's filters")
async def zm_filters(
    category: Category,
    spec: Annotated[
        str | None,
        Field(
            description="Show every value of one spec filter (part of its name, e.g. رم). Default lists the filters with a few values each."
        ),
    ] = None,
) -> dict:
    """Show what zm_list can filter by in a category: price range, brand slugs, spec filters with values, buying guides.

    Call before zm_list when the user asks for a brand, RAM, storage, 5G, screen size and the like.
    """
    c = await _category(category)
    specs = c["specifications"]
    if spec:
        specs = [s for s in specs if _norm(spec) in _norm(s["title"]) or spec == str(s["id"])]
        if not specs:
            raise ApiError(f"No filter matches '{spec}'. Call zm_filters without `spec` to list them.")
    rows = [
        {
            "name": s["title"],
            "id": s["id"],
            "values": [v["label"] for v in (s["values"] if spec else s["values"][:8])],
            **({} if spec or len(s["values"]) <= 8 else {"value_count": len(s["values"])}),
        }
        for s in specs
    ]
    return {
        "category": c["slug"],
        "title": c["title"],
        "price_range": [_price(c.get("minPrice")), _price(c.get("maxPrice"))],
        "brands": [{"slug": b["value"], "title": b["label"]} for b in c["brands"]] if not spec else None,
        "specs": rows,
        "buying_guides": [g["title"] for g in c["buyingGuides"]] if not spec else None,
    }


def _rows(product: dict) -> list[tuple[str, str, str]]:
    """(section, name, value) for every filled spec row of a product."""
    out = []
    for sec in product["info"].get("specificationCategories") or []:
        for s in sec["specifications"]:
            v = (s.get("value") or "").strip().replace("\n", " | ")
            if v:
                out.append((sec["title"], s["title"], v))
    return out


def _lines(text: str | None) -> list[str]:
    return [x.strip() for x in (text or "").split("\n") if x.strip()]


@tool("Get product specs")
async def zm_product(
    slug: Slug,
    sections: Annotated[
        list[str] | None,
        Field(
            description="Only these spec sections (part of the Persian name), e.g. ['باتری', 'دوربین']. Default all."
        ),
    ] = None,
) -> dict:
    """Get one product's full spec table, user score, Zoomit's pros and cons and its price range in Toman.

    Use after zm_search or zm_list. Next: zm_prices for sellers and price history, zm_similar for alternatives.
    """
    p = await _product(slug)
    info, stores = p["info"], p.get("stores") or {}
    ov = info.get("overview") or {}
    want = [_norm(s) for s in sections or []]
    table: dict[str, dict[str, str]] = {}
    for sec, name, v in _rows(p):
        if not want or any(w in _norm(sec) for w in want):
            table.setdefault(sec, {})[name] = v
    if sections and not table:
        names = ", ".join(s["title"] for s in info.get("specificationCategories") or [])
        raise ApiError(f"No spec section matches {sections}. Sections: {names}")
    return {
        "slug": info["slug"],
        "title": info["title"],
        "english": info.get("englishTitle"),
        "brand": info["brand"]["title"],
        "category": info["category"]["slug"],
        "user_score": (p.get("userReviewsInfo") or {}).get("averageScore") or None,
        "user_votes": (p.get("userReviewsInfo") or {}).get("usersCount"),
        "zoomit_score": ov.get("score") or None,
        "price_from": _price(stores.get("minPrice")),
        "price_to": _price(stores.get("maxPrice")),
        "sellers": stores.get("totalCount"),
        "in_stock": stores.get("inStock"),
        "key_specs": _specs_line(info),
        "pros": _lines(ov.get("pros")),
        "cons": _lines(ov.get("cons")),
        "review_url": ov.get("reviewArticleUrl"),
        "page": f"{SITE}/product/{info['slug']}/",
        "specs": table,
        "image": _img((info.get("gallery") or [{}])[0].get("image")),
    }


def _history(chart: dict) -> dict:
    """Summarise price-chart ticks: first/last/min/max and about a dozen evenly spaced points."""
    pts = []
    for seg in chart.get("ticks") or []:
        for t in seg:
            d = datetime.fromisoformat(t["date"].replace("Z", "+00:00")) + TEHRAN_UTC
            pts.append((d.strftime("%Y-%m-%d"), int(t["price"])))
    if not pts:
        return {"points": 0}
    lo, hi = min(pts, key=lambda x: x[1]), max(pts, key=lambda x: x[1])
    step = max(1, len(pts) // 11)
    sample = pts[::step]
    if sample[-1] != pts[-1]:
        sample.append(pts[-1])
    return {
        "points": len(pts),
        "first": {"date": pts[0][0], "price": pts[0][1]},
        "last": {"date": pts[-1][0], "price": pts[-1][1]},
        "lowest": {"date": lo[0], "price": lo[1]},
        "highest": {"date": hi[0], "price": hi[1]},
        "change_percent": round((pts[-1][1] - pts[0][1]) / pts[0][1] * 100, 1) if pts[0][1] else None,
        "sample": [list(x) for x in sample],
    }


@tool("Get sellers and price history")
async def zm_prices(
    slug: Slug,
    history: Annotated[
        Literal["none", "month", "3months", "6months", "year", "all"],
        Field(description="Price-history window; 'none' skips it."),
    ] = "3months",
    limit: Annotated[int, Field(description="How many seller offers to return, cheapest first.", ge=1, le=40)] = 10,
    city: Annotated[str | None, Field(description="Only sellers in this city (Persian), e.g. تهران.")] = None,
    registered_only: Annotated[
        bool,
        Field(
            description="Drop listings the shop marks as not registered (بدون رجیستری, grey market); unmarked ones stay."
        ),
    ] = False,
) -> dict:
    """List a product's seller offers (cheapest first, Toman, with the shop's own listing title and link) and its price history.

    Use for "where is it cheapest" or "has the price dropped". Prices come from the shops Zoomit tracks and change daily.
    Phones are often listed without registry (registered=false); use registered_only for the legal-to-use price.
    """
    p = await _product(slug)
    stores = p.get("stores") or {}
    items = stores.get("items") or []
    if (stores.get("totalCount") or 0) > len(items) and (city or registered_only or limit > len(items)):
        # The product page carries only the first 20 sellers; the stores endpoint has all of them.
        try:
            stores = await fetch(f"{CATALOG}/products/{p['id']}/stores", {"size": 100, "sort": "Cheapest"}) | {
                "physicalStores": stores.get("physicalStores")
            }
            items = stores.get("items") or items
        except ApiError:
            pass
    offers = []
    for s in items:
        if city and s.get("city") != city:
            continue
        for it in s.get("purchaseItems") or []:
            if registered_only and it.get("registry") is False:
                continue
            offers.append(
                {
                    "store": s["storeTitle"],
                    "price": _price(it.get("price")),
                    "listing": it.get("description"),
                    "registered": it.get("registry"),
                    "color": it.get("color"),
                    "guarantee": it.get("guarantee"),
                    "city": s.get("city"),
                    "condition": s.get("productCondition"),
                    "store_score": (s.get("userReviewsInfo") or {}).get("averageScore") or None,
                    "url": s.get("url"),
                }
            )
    offers = sorted((o for o in offers if o["price"]), key=lambda o: o["price"])
    physical = [
        {
            "store": x["storeTitle"],
            "city": x.get("city"),
            "price": _price(x.get("price")),
            "address": x.get("addressDescription"),
        }
        for x in stores.get("physicalStores") or []
    ]
    out: dict[str, Any] = {
        "slug": p["info"]["slug"],
        "title": p["info"]["title"],
        "currency": "Toman",
        "sellers_total": stores.get("totalCount"),
        "price_from": _price(stores.get("minPrice")),
        "price_to": _price(stores.get("maxPrice")),
        "offers": offers[:limit],
        "physical_stores": physical,
    }
    if history != "none" and p.get("hasPriceChart"):
        params = {"timeRange": RANGES[history]} if RANGES[history] else None
        out["history"] = _history(await fetch(f"{CATALOG}/products/{p['id']}/price-chart", params))
    return out


@tool("Compare products side by side")
async def zm_compare(
    slugs: Annotated[
        list[str], Field(description="2 to 4 product slugs from zm_search or zm_list.", min_length=2, max_length=4)
    ],
    only_differences: Annotated[
        bool, Field(description="Hide spec rows where every product has the same value.")
    ] = True,
) -> dict:
    """Compare 2-4 products: price, scores and a spec table where each row lists the values in the order of `products`.

    Use for "A or B". Compare products of the same category for matching rows.
    """
    prods = await asyncio.gather(*(_product(s) for s in slugs))
    names = [p["info"]["title"] for p in prods]
    table: dict[tuple[str, str], list[str | None]] = {}
    for i, p in enumerate(prods):
        for sec, name, v in _rows(p):
            table.setdefault((sec, name), [None] * len(prods))[i] = v
    sections: dict[str, list[list[str | None]]] = {}
    same = 0
    for (sec, name), vals in table.items():
        if only_differences and len(set(vals)) == 1:
            same += 1
            continue
        sections.setdefault(sec, []).append([name, *vals])
    return {
        "products": [
            {
                "slug": p["info"]["slug"],
                "title": t,
                "price_from": _price((p.get("stores") or {}).get("minPrice")),
                "user_score": (p.get("userReviewsInfo") or {}).get("averageScore") or None,
                "zoomit_score": (p["info"].get("overview") or {}).get("score") or None,
            }
            for p, t in zip(prods, names, strict=True)
        ],
        "same_rows_hidden": same,
        "specs": sections,
    }


@tool("Find similar products")
async def zm_similar(
    slug: Slug,
    kind: Annotated[
        Literal["compared", "brand", "price"],
        Field(description="compared = most compared with it; brand = same brand; price = similar price."),
    ] = "compared",
    limit: Annotated[int, Field(description="How many products to return.", ge=1, le=20)] = 8,
) -> dict:
    """Suggest alternatives to a product: the ones people compare it with, others of its brand, or in its price range.

    Next: zm_compare with the product and a few results.
    """
    p = await _product(slug)
    pid, info, stores = p["id"], p["info"], p.get("stores") or {}
    if kind == "compared":
        data = await fetch(f"{CATALOG}/comparisons/related/{pid}", {"size": limit})
    elif kind == "brand":
        q = {
            "id": pid,
            "BrandId": info["brand"]["id"],
            "CategoryId": info["category"]["id"],
            "Size": limit,
            "ProductId": pid,
        }
        data = await fetch(f"{CATALOG}/products/{pid}/related/brand", q)
    else:
        # The site passes the product's whole seller price range; a band around its lowest price is more useful.
        low = int(stores.get("minPrice") or 0)
        q = {
            "id": pid,
            "MinPrice": int(low * 0.8),
            "MaxPrice": int(low * 1.25),
            "CategoryId": info["category"]["id"],
            "Size": limit,
            "ProductId": pid,
        }
        data = await fetch(f"{CATALOG}/products/{pid}/related/price", q)
    return {"of": info["slug"], "kind": kind, "products": [_card(x) for x in data][:limit]}


@tool("Search Zoomit articles")
async def zm_articles(
    query: Annotated[
        str, Field(description="Topic or product name, e.g. بررسی آیفون 17 پرو.", min_length=1, max_length=100)
    ],
    limit: Annotated[int, Field(description="How many articles to return.", ge=1, le=20)] = 5,
) -> dict:
    """Search Zoomit's news and review articles (title, lead, date, link).

    Use to find the full written review or news about a product; zm_product also returns review_url when one exists.
    """
    data = (await fetch(f"{EDITORIAL}/search/v2", {"Query": query, "Articles.PageSize": limit}))["articles"]
    return {
        "total": data["totalCount"],
        "articles": [
            {
                "title": a["title"],
                "lead": a.get("lead"),
                "published": (a.get("publishedDate") or "")[:10],
                "author": (a.get("author") or {}).get("fullName", "").strip() or None,
                "comments": a.get("totalDiscussCount"),
                "url": f"{SITE}/{a['slug']}/",
            }
            for a in data["source"]
        ],
    }
