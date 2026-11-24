"""IBFC-first system grades with the existing raw-score and tie mapping."""
from decimal import Decimal, ROUND_HALF_UP


STEP = Decimal("0.1")
COMPARISON_STEP = Decimal("1e-20")
SYSTEMS = ("digestive", "metabolic", "cardiovascular", "neurological",
           "psychiatric", "immune", "urinary", "oncology")
# Upper bound, inclusive, high count, middle count. Counts are product rules,
# not calibrated disease probabilities. Zero retains all 23 low-risk slots.
_COUNT_BANDS = (
    ("1", False, 2, 2), ("2", False, 2, 3), ("2.5", False, 2, 4),
    ("3", False, 2, 7), ("4", False, 3, 7), ("4.5", True, 4, 7),
    ("5", False, 5, 8), ("6", False, 6, 8), ("6.5", True, 7, 8),
    ("7", False, 8, 11), ("8", False, 10, 9), ("9", False, 12, 8),
    ("10", False, 14, 7), ("10", True, 16, 6), ("15", False, 18, 5),
)


def system_grade_policy(ibfc):
    """Fixed counts determined solely by IBFC."""
    value = Decimal(str(ibfc))
    if not value.is_finite() or value < 0:
        raise ValueError("IBFC must be finite and nonnegative")
    high = dict.fromkeys(SYSTEMS, 0)
    non_low = dict.fromkeys(SYSTEMS, 0)
    middle = dict.fromkeys(SYSTEMS, 0)
    eligible = set(SYSTEMS)
    if value == 0:
        high_count, middle_count, band = 0, 0, "zero"
    else:
        high_count, middle_count, band = 23, 0, "15_or_above"
        for upper, inclusive, h, m in _COUNT_BANDS:
            bound = Decimal(upper)
            if value < bound or inclusive and value == bound:
                high_count, middle_count = h, m
                band = "up_to_" + upper if inclusive else "below_" + upper
                break
        high.update(digestive=1, metabolic=1)
        non_low.update(digestive=1, metabolic=1)
        eligible = {"digestive", "metabolic"}
        if value >= Decimal("2.5"):
            non_low.update(cardiovascular=3, neurological=2)
            middle.update(cardiovascular=3, neurological=2)
        if value > Decimal("4.5"):
            high.update(digestive=2, cardiovascular=1, neurological=1)
            non_low.update(digestive=4, metabolic=3, cardiovascular=4, neurological=2)
            middle.update(cardiovascular=0, neurological=0)
            eligible.update(("cardiovascular", "neurological"))
        if value > Decimal("6.5"):
            high.update(psychiatric=1, immune=1, oncology=1)
            non_low.update(psychiatric=2, immune=2, oncology=2)
            middle.update(psychiatric=1, immune=1, oncology=1)
            eligible = set(SYSTEMS)
        if value > 10:
            middle = dict.fromkeys(SYSTEMS, 0)
    return {"band": band, "highCount": high_count, "middleCount": middle_count,
            "systemHighQuotas": high, "systemMinimumNonLow": non_low,
            "systemMinimumMiddle": middle,
            "highEligibleSystems": [system for system in SYSTEMS if system in eligible]}


def high_quota(ibfc):
    return system_grade_policy(ibfc)["highCount"]


def _high_completion_possible(counts, capacities, policy):
    """DP checks exact high count and enough room for required middle entries.

    This prevents a high candidate in an unrelated system from consuming a
    middle slot needed by a required system at the tight IBFC boundaries.
    """
    target, middle_target = policy["highCount"], policy["middleCount"]
    costs = {0: 0}
    for system in SYSTEMS:
        next_costs = {}
        for total, cost in costs.items():
            for high in range(counts[system], min(capacities[system], target - total) + 1):
                required = max(0, policy["systemMinimumNonLow"][system] - high,
                               policy["systemMinimumMiddle"][system])
                next_total, next_cost = total + high, cost + required
                next_costs[next_total] = min(next_costs.get(next_total, 24), next_cost)
        costs = next_costs
    return costs.get(target, 24) <= middle_target


def mapped_score(raw, level):
    raw = Decimal(str(raw))
    anchor, origin, width = {
        "高": ("6.7", "3", "5"), "中": ("3.4", "1.5", "4"),
        "低": ("1", "1", "1.2"),
    }[level]
    fraction = max(Decimal(0), min(Decimal(1), (raw - Decimal(origin)) / Decimal(width)))
    scale = Decimal("2.3") if level == "低" else Decimal("3.2")
    return (Decimal(anchor) + scale * fraction).quantize(STEP, rounding=ROUND_HALF_UP)


def separate_scores(ranked_scores, level):
    """Round, move collisions downward, then lift from the floor if necessary.

    Input is already in descending raw/catalog order. The backward pass raises
    only values needed to fit every entry above the band's lower bound.
    """
    lower, upper = {"高": (Decimal("6.7"), Decimal("9.9")),
                    "中": (Decimal("3.4"), Decimal("6.6")),
                    "低": (Decimal("1"), Decimal("3.3"))}[level]
    if len(ranked_scores) > int((upper - lower) / STEP) + 1:
        raise ValueError("risk band cannot contain all distinct scores")
    values = []
    for raw in ranked_scores:
        value = mapped_score(raw, level)
        if values:
            value = min(value, values[-1] - STEP)
        values.append(value)
    for index in range(len(values) - 1, -1, -1):
        minimum = lower if index == len(values) - 1 else values[index + 1] + STEP
        values[index] = max(values[index], minimum)
    if values and values[0] > upper:
        raise ValueError("risk band overflow")
    return values


def grade_items(items, ibfc, raw_scores):
    """Assign fixed IBFC grades and map the same raw score inside each grade."""
    order = {item["code"]: index for index, item in enumerate(items)}
    by_code = {item["code"]: item for item in items}

    def ranked_by(scores):
        return sorted(by_code, key=lambda code: (
            -scores[code].quantize(COMPARISON_STEP, rounding=ROUND_HALF_UP), order[code],
        ))

    ranked = ranked_by(raw_scores)
    policy = system_grade_policy(ibfc)
    high_count, medium_count = policy["highCount"], policy["middleCount"]
    sizes = {system: sum(row["systemCode"] == system for row in items) for system in SYSTEMS}
    if len(items) != 23 or any(policy["systemMinimumNonLow"][s] > sizes[s] for s in SYSTEMS):
        raise ValueError("risk grade policy requires the complete 23-item catalog")
    capacities = {s: sizes[s] - policy["systemMinimumMiddle"][s]
                  if s in policy["highEligibleSystems"] else 0 for s in SYSTEMS}
    high, counts = set(), dict.fromkeys(SYSTEMS, 0)
    for system, count in policy["systemHighQuotas"].items():
        selected = [code for code in ranked if by_code[code]["systemCode"] == system][:count]
        high.update(selected)
        counts[system] = len(selected)
    if not _high_completion_possible(counts, capacities, policy):
        raise ValueError("infeasible risk system grade counts")
    for code in ranked:
        if len(high) == high_count:
            break
        system = by_code[code]["systemCode"]
        if code in high or counts[system] >= capacities[system]:
            continue
        counts[system] += 1
        if _high_completion_possible(counts, capacities, policy):
            high.add(code)
        else:
            counts[system] -= 1
    medium = set()
    for system in SYSTEMS:
        required = max(0, policy["systemMinimumNonLow"][system] - counts[system],
                       policy["systemMinimumMiddle"][system])
        medium.update([code for code in ranked if code not in high
                       and by_code[code]["systemCode"] == system][:required])
    medium.update([code for code in ranked if code not in high | medium][:medium_count - len(medium)])
    if len(high) != high_count or len(medium) != medium_count or high & medium:
        raise ValueError("risk grade count mismatch")
    for level, selected in (("高", high), ("中", medium), ("低", set(ranked) - high - medium)):
        codes = [code for code in ranked if code in selected]
        scores = separate_scores([raw_scores[code] for code in codes], level)
        for code, score in zip(codes, scores):
            mapped = mapped_score(raw_scores[code], level)
            by_code[code].update(riskIndex=float(score), riskLevel=level,
                                 grading={"level": level, "mappedScore": float(mapped),
                                          "tieAdjustment": float(score - mapped)})
    return {"highQuota": high_count, "mediumQuota": medium_count,
            "ibfcBand": policy["band"], "systemHighQuotas": policy["systemHighQuotas"],
            "systemMinimumNonLow": policy["systemMinimumNonLow"],
            "systemMinimumMiddle": policy["systemMinimumMiddle"],
            "highCodes": [code for code in ranked if code in high],
            "mediumCodes": [code for code in ranked if code in medium],
            "lowCodes": [code for code in ranked if code not in high | medium],
            "floorEvents": [], "minimumLowCount": 23 - high_count - medium_count}
