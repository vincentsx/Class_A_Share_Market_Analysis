"""Descriptive distributions, benchmark comparison and explicit signal cohorts."""

from collections import defaultdict
from statistics import mean, median


def percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * fraction
    low = int(index)
    return ordered[low] + (ordered[min(low + 1, len(ordered) - 1)] - ordered[low]) * (index - low)


def summarize(items, horizon):
    key = str(horizon)
    observations = [i["horizons"][key] for i in items if i["horizons"].get(key, {}).get("status") == "ready"]
    values = [o["return"] for o in observations]
    excess = [o["excess_return"] for o in observations if o.get("excess_return") is not None]
    invalidated = sum(bool(i["invalidated_on"] and i["invalidated_on"] <= i["horizons"][key]["end_day"])
                      for i in items if i["horizons"].get(key, {}).get("status") == "ready")
    buckets = [sum(v < -5 for v in values), sum(-5 <= v < 0 for v in values),
               sum(0 <= v < 5 for v in values), sum(v >= 5 for v in values)]
    return {"horizon": horizon, "count": len(values), "total": len(items),
            "pending": sum(i["horizons"].get(key, {}).get("status") == "pending" for i in items),
            "missing": sum(i["horizons"].get(key, {}).get("status") not in {"ready", "pending"} for i in items),
            "mean_return": mean(values) if values else None, "median_return": median(values) if values else None,
            "p25": percentile(values, .25), "p75": percentile(values, .75),
            "positive_ratio": sum(v > 0 for v in values) / len(values) * 100 if values else None,
            "mean_adverse": mean(o["max_adverse"] for o in observations) if observations else None,
            "mean_excess": mean(excess) if excess else None, "benchmark_count": len(excess),
            "invalidation_ratio": invalidated / len(values) * 100 if values else None,
            "distribution": dict(zip(["<-5%", "-5%至0%", "0%至5%", "≥5%"], buckets)),
            "small_sample": len(values) < 30}


def cohorts(items, dates):
    positions = {d: i for i, d in enumerate(dates)}
    last = {}
    independent = []
    # Cooldown is forward-only from the first signal, never selects using future returns.
    published = [i for i in items if not i.get("reconstructed")]
    for item in sorted(published, key=lambda i: (i["day"], i["code"], i["strategy"])):
        key = (item["code"], item["strategy"], item["rule_version"])
        index = positions.get(item["day"])
        if index is not None and (key not in last or index - last[key] >= 20):
            independent.append(item)
            last[key] = index
    result = []
    for cohort, members in [("all", published), ("priority", [i for i in published if i["priority"]]),
                            ("cooldown20", independent), ("reconstructed", [i for i in items if i.get("reconstructed")])]:
        groups = defaultdict(list)
        for item in members:
            groups[(item["strategy"], item["rule_version"])].append(item)
        for (strategy, version), group in sorted(groups.items()):
            result.append({"cohort": cohort, "strategy": strategy, "rule_version": version,
                           "label": group[0]["label"], "count": len(group),
                           "summary": [summarize(group, n) for n in (5, 10, 20)]})
    return result
