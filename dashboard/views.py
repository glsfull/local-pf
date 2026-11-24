"""Data shaping for the TopPult-style dashboard pages."""
from datetime import date, timedelta

from .forms import brand_list
from .history import visibility, visibility_weight

COMPETITION_FLAMES = {"low": 1, "medium": 2, "high": 3}
AUTO_POWER = {"low": 10, "medium": 20, "high": 30}
LOW_FREQUENCY = 10
KEYWORD_FILTERS = {
    "": "Без фильтров",
    "top10": "В топ-10",
    "top20": "В топ-20",
    "outside": "Вне топ-20",
    "unchecked": "Без позиции",
    "errors": "С ошибками проверки",
}
KEYWORD_SORTS = ("position", "chance", "iw", "demand", "power", "visits", "budget")
PAGE_SIZES = (15, 30, 50, 100)
VISIT_BRANDS = {"": "Все", "nobrand": "Без брендовых запросов", "brand": "Только с брендовыми"}


def day_visibility(lines, day):
    ranks = [
        sample["rank"] for line in lines if not line["hinted"]
        if (sample := line["days"].get(day)) and not sample["error"]
    ]
    return sum(map(visibility_weight, ranks)) / len(ranks) if ranks else None


def project_visibility(days, lines):
    """Visibility of the latest measured day and its change against the previous one."""
    measured = [day for day in days if day_visibility(lines, day) is not None]
    if not measured:
        return None, None
    current = day_visibility(lines, measured[0])
    previous = day_visibility(lines, measured[1]) if len(measured) > 1 else None
    return current, None if previous is None else current - previous


def keyword_position(sample):
    if not sample:
        return None
    return sample["rank"] or 51


def keyword_rows(words, latest, queue, flt, sort, descending):
    rows = []
    for word in words:
        sample = latest.get((word["phrase"], word["landing_url"]))
        rows.append({
            "word": word, "sample": sample, "position": None if sample and sample["error"] else keyword_position(sample),
            "queue": queue.get(word["phrase"], 0), "flames": COMPETITION_FLAMES.get(word["competition"] or "", 0),
        })
    if flt == "top10":
        rows = [row for row in rows if row["position"] and row["position"] <= 10]
    elif flt == "top20":
        rows = [row for row in rows if row["position"] and row["position"] <= 20]
    elif flt == "outside":
        rows = [row for row in rows if row["position"] and row["position"] > 20]
    elif flt == "unchecked":
        rows = [row for row in rows if not row["sample"]]
    elif flt == "errors":
        rows = [row for row in rows if row["sample"] and row["sample"]["error"]]

    def value(row):
        return row["position"] if sort == "position" else row["word"][sort]

    present = [row for row in rows if value(row) is not None]
    missing = [row for row in rows if value(row) is None]
    present.sort(key=value, reverse=descending)
    return present + missing


def keyword_totals(rows):
    def total(key, source="word"):
        values = [row[source][key] if source else row[key] for row in rows]
        values = [value for value in values if value is not None]
        return sum(values) if values else None

    return {
        "iw": total("iw"), "demand": total("demand"), "visits": total("visits"),
        "queue": total("queue", None), "budget": total("budget"),
    }


def warnings(balance, words):
    items = []
    if balance is not None and balance <= 0:
        items.append((
            "На балансе недостаточно средств. Переходы по проектам приостановлены.",
            "Пополните баланс в настройках панели, чтобы продолжить продвижение.",
        ))
    chances = [word["chance"] for word in words if word["chance"] is not None]
    if chances and (chance := sum(chances) / len(chances)) < 20:
        items.append((
            "Сайт не готов для ПФ-продвижения.",
            f"Общий шанс успеха продвижения низкий ({round(chance)}%) — либо на сайте ещё нет качественных "
            "релевантных страниц под указанные запросы, либо у сайта есть технические ошибки.",
        ))
    return items


def cluster_tree(words, details):
    clusters = {}
    for word in words:
        clusters.setdefault(word["cluster"] or "", []).append(word)
    tree = []
    for name, items in clusters.items():
        frequencies = [word["iw"] for word in items if word["iw"] is not None]
        tree.append({
            "name": name, "words": items, "details": details.get(name, {}),
            "iw": sum(frequencies) if frequencies else None,
            "urls": sorted({word["landing_url"] for word in items if word["landing_url"]}),
        })
    named = sorted((item for item in tree if item["name"]), key=lambda item: -(item["iw"] or 0))
    return named, next((item for item in tree if not item["name"]), None)


def recluster(words, lines):
    """Group phrases by the page the search returned for them, like SERP-based clustering."""
    found = {}
    for line in lines:
        if not line["days"]:
            continue
        sample = line["days"][max(line["days"])]
        key = (line["phrase"], line["landing_url"])
        if sample["url"] and (key not in found or (not line["hinted"])):
            found[key] = sample["url"]
    groups = {}
    for word in words:
        url = found.get((word["phrase"], word["landing_url"]))
        if url:
            groups.setdefault(url, []).append(word)
    clusters = {}
    for url, items in groups.items():
        leader = max(items, key=lambda word: (word["iw"] or 0, -word["ordinal"]))
        for word in items:
            clusters[word["ordinal"]] = (leader["phrase"][:100], url)
    return clusters


def period_days(args, today):
    """Return (since, until, limit, period) for the period filters."""
    since = until = None
    for key in ("from", "to"):
        value = args.get(key, "")
        try:
            parsed = date.fromisoformat(value) if value else None
        except ValueError:
            parsed = None
        if key == "from":
            since = parsed
        else:
            until = parsed
    if since or until:
        return since, until, 92, "custom"
    period = "14" if args.get("period") == "14" else "30"
    return None, None, int(period), period


def trim_days(days, period):
    """Keep the calendar period ending on the latest measured day (days are sorted newest first)."""
    if period == "custom" or not days:
        return days
    return [day for day in days if day > days[0] - timedelta(days=int(period))]


def page_slice(items, args):
    try:
        per_page = int(args.get("per_page", PAGE_SIZES[0]))
    except ValueError:
        per_page = PAGE_SIZES[0]
    if per_page not in PAGE_SIZES:
        per_page = PAGE_SIZES[0]
    pages = max(1, -(-len(items) // per_page))
    try:
        page = min(max(1, int(args.get("page", 1))), pages)
    except ValueError:
        page = 1
    start = (page - 1) * per_page
    return items[start:start + per_page], {
        "page": page, "pages": pages, "per_page": per_page, "total": len(items),
        "first": start + 1 if items else 0, "last": min(start + per_page, len(items)),
    }


def is_brand(phrase, brands):
    folded = phrase.casefold()
    return any(brand.casefold() in folded for brand in brands if brand)


def visit_matrix(db, project_id, words, settings, latest, args, today, *, paginate=True):
    since, until, limit, period = period_days(args, today)
    rows = db.execute(
        "SELECT * FROM visit_stats WHERE project_id = ? ORDER BY day DESC, phrase", (project_id,),
    ).fetchall()
    days = trim_days(sorted({
        date.fromisoformat(row["day"]) for row in rows
        if (since is None or row["day"] >= since.isoformat()) and (until is None or row["day"] <= until.isoformat())
    }, reverse=True)[:limit], period)
    current = {word["phrase"] for word in words}
    brands = brand_list(settings) + [word["brand"] for word in words if word["brand"]]
    urls = {}
    for (phrase, _), sample in latest.items():
        if sample["url"]:
            urls.setdefault(phrase, sample["url"])
    mode = args.get("brand", "") if args.get("brand", "") in VISIT_BRANDS else ""
    deleted = args.get("deleted") == "1"
    shown = set(day.isoformat() for day in days)
    lines = {}
    for word in words:
        lines.setdefault(word["phrase"], {"phrase": word["phrase"], "deleted": False, "days": {}})
    for row in rows:
        if row["phrase"] not in current and not deleted:
            continue
        line = lines.setdefault(row["phrase"], {"phrase": row["phrase"], "deleted": row["phrase"] not in current, "days": {}})
        if row["day"] in shown:
            line["days"][row["day"]] = row
    result = []
    for line in lines.values():
        if mode == "brand" and not is_brand(line["phrase"], brands):
            continue
        if mode == "nobrand" and is_brand(line["phrase"], brands):
            continue
        line["cost"] = sum(cell["cost"] for cell in line["days"].values())
        line["url"] = urls.get(line["phrase"])
        result.append(line)
    totals = {}
    for day in days:
        cells = [line["days"][day.isoformat()] for line in result if day.isoformat() in line["days"]]
        done = sum(cell["done"] for cell in cells)
        depth = [cell["depth"] * cell["done"] for cell in cells if cell["depth"] is not None]
        totals[day.isoformat()] = {
            "done": done, "planned": sum(cell["planned"] for cell in cells),
            "cost": sum(cell["cost"] for cell in cells), "depth": sum(depth) / done if done and depth else None,
        }
    summary = {
        "done": sum(item["done"] for item in totals.values()),
        "planned": sum(item["planned"] for item in totals.values()),
        "cost": sum(item["cost"] for item in totals.values()),
    }
    page, pagination = page_slice(result, args) if paginate else (result, None)
    return {
        "days": days, "lines": page, "totals": totals, "summary": summary, "pagination": pagination,
        "period": period, "since": since, "until": until, "brand": mode, "deleted": deleted,
    }


def visit_class(cell):
    if cell is None:
        return ""
    if cell["done"] == 0:
        return "visit-miss"
    if cell["depth"] is not None and cell["depth"] >= 3:
        return "visit-high"
    return "visit-ok"


def expenses(db, project_id, settings):
    rows = db.execute(
        """SELECT day, SUM(done) AS done, SUM(planned) AS planned, SUM(cost) AS cost
           FROM visit_stats WHERE project_id = ? GROUP BY day ORDER BY day DESC LIMIT 92""",
        (project_id,),
    ).fetchall()
    limit = settings["daily_budget"] if settings["limit_budget"] else None
    return [
        {**dict(row), "over": limit is not None and row["cost"] > limit} for row in rows
    ], {
        "done": sum(row["done"] for row in rows), "planned": sum(row["planned"] for row in rows),
        "cost": sum(row["cost"] for row in rows),
    }


def today_queue(db, project_id, today):
    return {
        row["phrase"]: max(row["planned"] - row["done"], 0) for row in db.execute(
            "SELECT phrase, planned, done FROM visit_stats WHERE project_id = ? AND day = ?",
            (project_id, today.isoformat()),
        )
    }


def low_frequency_skip(words, lines, today, days=3):
    """Ordinals of rare phrases measured recently; scheduled runs check them less often than daily."""
    recent = set()
    for line in lines:
        if not line["hinted"] and any(today - day < timedelta(days=days) for day in line["days"]):
            recent.add((line["phrase"], line["landing_url"]))
    return {
        word["ordinal"] for word in words
        if word["iw"] is not None and word["iw"] < LOW_FREQUENCY and (word["phrase"], word["landing_url"]) in recent
    }
