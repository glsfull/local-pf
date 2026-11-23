from collections import Counter
from datetime import datetime

from modules.clock import MSK

TIERS = ("top-1", "top-3", "top-10", "top-50", "outside")
TIER_LABELS = dict(zip(TIERS, ("Топ-1", "Топ-3", "Топ-10", "Топ-50", "51+")))


def moscow_date(value):
    if not isinstance(value, str):
        return None
    try:
        date = datetime.fromisoformat(value)
    except ValueError:
        return None
    return (date if date.tzinfo else date.replace(tzinfo=MSK)).astimezone(MSK)


def rank_tier(rank):
    if rank is None:
        return "outside"
    return next((tier for limit, tier in zip((1, 3, 10, 50), TIERS) if rank <= limit), "outside")


def read_history(db, project_id):
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

    days = sorted({day for line in series.values() for day in line["days"]}, reverse=True)[:30]
    lines = sorted(
        (line for line in series.values() if any(day in line["days"] for day in days)),
        key=lambda line: (line["phrase"].casefold(), line["landing_url"], line["hinted"], line["query"]),
    )
    return days, lines, latest


def visibility(days, lines, hinted):
    chart = []
    for day in reversed(days):
        counts = Counter(
            rank_tier(sample["rank"]) for line in lines if line["hinted"] == hinted
            if (sample := line["days"].get(day)) and not sample["error"]
        )
        chart.append({
            "day": day, "counts": [counts[tier] for tier in TIERS], "total": sum(counts.values()),
        })
    return chart
