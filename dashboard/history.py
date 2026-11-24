from collections import Counter
from datetime import datetime

from modules.clock import MSK

# Position buckets used by the matrix colours, the chart and the legend (TopPult palette).
TIERS = ("top-3", "top-6", "top-10", "top-20", "outside")
TIER_LIMITS = (3, 6, 10, 20)
TIER_LABELS = dict(zip(TIERS, ("Топ 1-3", "Топ 4-6", "Топ 7-10", "Топ 11-20", "Топ 21+")))
# Share of clicks a result gets on a given position of the first page, in percent.
VISIBILITY_WEIGHTS = (100, 85, 60, 50, 50, 30, 30, 20, 20, 20)


def moscow_date(value):
    if not isinstance(value, str):
        return None
    try:
        date = datetime.fromisoformat(value)
    except ValueError:
        return None
    return (date if date.tzinfo else date.replace(tzinfo=MSK)).astimezone(MSK)


def rank_tier(rank):
    """Map a position to its CSS class: 1-3, 4-6, 7-10, 11-20 and 21+ (including 51+ and not found)."""
    if rank is None:
        return "outside"
    return next((tier for limit, tier in zip(TIER_LIMITS, TIERS) if rank <= limit), "outside")


def visibility_weight(rank):
    return VISIBILITY_WEIGHTS[rank - 1] if rank and rank <= len(VISIBILITY_WEIGHTS) else 0


def read_history(db, project_id, *, since=None, until=None, limit=30):
    rows = db.execute(
        """SELECT m.* FROM measurements m JOIN runs r ON r.id = m.run_id
           WHERE r.project_id = ? AND (m.checked_at IS NOT NULL OR m.hinted_at IS NOT NULL)
           ORDER BY r.id DESC, m.ordinal""",
        (project_id,),
    )
    series, latest = {}, {}
    for row in rows:
        for hinted in (False, True):
            rank, url, at, error = (
                (row["hint_rank"], row["hint_found_url"], row["hinted_at"], row["hint_error"]) if hinted else
                (row["rank"], row["found_url"], row["checked_at"], row["error"])
            )
            measured = moscow_date(at)
            if measured is None:
                continue
            key = (row["phrase"], row["landing_url"])
            query = row["hint_query"] if hinted else row["phrase"]
            line = series.setdefault((*key, hinted, query), {
                "phrase": row["phrase"], "query": query, "hinted": hinted, "landing_url": row["landing_url"],
                "iw": row["iw"], "url": None, "days": {},
            })
            sample = {"rank": rank, "at": measured, "error": error, "url": url}
            if not hinted and (key not in latest or latest[key]["at"] < measured):
                latest[key] = sample
            day = measured.date()
            saved = line["days"].get(day)
            if saved is None or saved["at"] < measured:
                line["days"][day] = sample
            if line["url"] is None and url is not None:
                line["url"] = url

    days = sorted((
        day for day in {day for line in series.values() for day in line["days"]}
        if (since is None or day >= since) and (until is None or day <= until)
    ), reverse=True)[:limit]
    lines = sorted(
        (line for line in series.values() if any(day in line["days"] for day in days)),
        key=lambda line: (line["phrase"].casefold(), line["landing_url"], line["hinted"], line["query"]),
    )
    return days, lines, latest


def visibility(days, lines, hinted):
    chart = []
    for day in reversed(days):
        ranks = [
            sample["rank"] for line in lines if line["hinted"] == hinted
            if (sample := line["days"].get(day)) and not sample["error"]
        ]
        counts = Counter(rank_tier(rank) for rank in ranks)
        total = len(ranks)
        chart.append({
            "day": day, "counts": [counts[tier] for tier in TIERS], "total": total,
            "top10": 100 * sum(1 for rank in ranks if rank and rank <= 10) / total if total else None,
            "visibility": sum(map(visibility_weight, ranks)) / total if total else None,
        })
    return chart
