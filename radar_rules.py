"""
Rule-based Event Radar: deterministic Gastrobar ranking + category guarantees.

Gemini = scout; radar_rules = editor (inclusion, score, guarantees).
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any, Literal

from event_verifier import gastrobar_hard_reject

log = logging.getLogger(__name__)

WatchTier = Literal["high", "medium", "low", "skip"]
CategoryBucket = Literal[
    "football", "hockey", "esports", "formula1", "basketball", "ufc", "tennis", "other"
]

SPORT_EMOJI: dict[str, str] = {
    "football": "⚽",
    "hockey": "🏒",
    "formula1": "🏎",
    "f1": "🏎",
    "esports": "🎮",
    "basketball": "🏀",
    "nba": "🏀",
    "tennis": "🎾",
    "mma": "🥊",
    "ufc": "🥊",
    "boxing": "🥊",
}

_WEEK_MIN_PER_CATEGORY: dict[CategoryBucket, int] = {
    "football": 2,
    "hockey": 2,
    "esports": 2,
    "formula1": 2,
    "basketball": 1,
    "ufc": 1,
    "tennis": 0,
    "other": 0,
}

_NOW24_MIN_PER_CATEGORY: dict[CategoryBucket, int] = {
    "football": 1,
    "hockey": 1,
    "esports": 1,
    "formula1": 1,
    "basketball": 1,
    "ufc": 1,
    "tennis": 0,
    "other": 0,
}

# Лимит слотов на категорию (чтобы F1 practice не забивали всю афишу).
_WEEK_CAT_CAP: dict[CategoryBucket, int] = {
    "football": 14,
    "hockey": 12,
    "esports": 8,
    "formula1": 6,
    "basketball": 5,
    "tennis": 4,
    "ufc": 3,
    "other": 2,
}
_NOW24_CAT_CAP: dict[CategoryBucket, int] = {
    "football": 8,
    "hockey": 6,
    "esports": 5,
    "formula1": 4,
    "basketball": 3,
    "tennis": 2,
    "ufc": 2,
    "other": 1,
}

_FOOTBALL_LEAGUE_RE = re.compile(
    r"\b(premier\s+league|\bepl\b|champions\s+league|\bucl\b|europa\s+league|"
    r"conference\s+league|bundesliga|serie\s+a|la\s+liga|laliga|ligue\s+1|"
    r"\brpl\b|российск|premier\s+liga|cup\s+final|fa\s+cup|copa\s+del\s+rey|"
    r"dfb[\s-]?pokal|coppa\s+italia|coupe\s+de\s+france|final\s+day|matchday\s+38)\b",
    re.I,
)
_FOOTBALL_TOP_CLUBS_RE = re.compile(
    r"\b(arsenal|liverpool|chelsea|manchester\s+city|man\s+city|manchester\s+united|"
    r"man\s+united|tottenham|bayern|real\s+madrid|barcelona|atletico|inter|ac\s+milan|"
    r"juventus|napoli|psg|paris\s+saint|marseille|monaco|zenit|spartak|cska|lokomotiv)\b",
    re.I,
)
_HOCKEY_KHL_RE = re.compile(
    r"\b(khl\b|kontinental\s+hockey|ак\s+барс|khl\s+final)\b",
    re.I,
)
_HOCKEY_NHL_RE = re.compile(
    r"\b(nhl\b|national\s+hockey\s+league|stanley\s+cup)\b",
    re.I,
)
_HOCKEY_WORLD_RE = re.compile(
    r"\b(world\s+championship|iihf|world\s+cup.*hockey)\b",
    re.I,
)
_HOCKEY_NATION_RE = re.compile(
    r"\b(canada|usa|united\s+states|finland|sweden|czech|czechia|latvia|slovakia|"
    r"germany|norway|denmark|switzerland|france|austria|italy|poland)\b",
    re.I,
)
_ESPORTS_RE = re.compile(
    r"\b(cs2|counter-strike|dota\s*2?|dreamleague|dream\s+league|"
    r"\biem\b|esl\b|blast|betboom|team\s+liquid|liquid\b|falcons|navi|na'vi|"
    r"tundra|virtus\.?pro|\bvp\b|mouz|spirit|vitality|g2|heroic|parivision|"
    r"major|pgl|playoff|grand\s+final|lan\s+final)\b",
    re.I,
)
_ESPORTS_TEAMS_RE = re.compile(
    r"\b(falcons|spirit|navi|na'vi|tundra|liquid|vitality|g2|mouz|vp|virtus|"
    r"heroic|parivision|betboom|mouz)\b",
    re.I,
)
_DREAMLEAGUE_RE = re.compile(r"\bdreamleague|dream\s+league\b", re.I)
_DOTA_CS_RE = re.compile(r"\b(dota\s*2?|counter-strike|cs2)\b", re.I)
_TENNIS_STARS_RE = re.compile(
    r"\b(tommy\s+paul|altmaier|medvedev|alcaraz|sinner|djokovic|zverev|rune|"
    r"rublev|tsitsipas|ruud|fritz|de\s+minaur|shelton|tiafoe|pegula|sabalenka|"
    r"swiatek|gauff)\b",
    re.I,
)
_F1_RE = re.compile(
    r"\b(formula\s*1|\bf1\b|grand\s+prix|practice\s*[123]?|fp[123]|"
    r"qualifying|sprint\s+qualifying|\bsprint\b|\brace\b)\b",
    re.I,
)
_F1_CORE_SESSION_RE = re.compile(
    r"\b(sprint\s+qualifying|qualifying|\bsprint\b|\brace\b)\b",
    re.I,
)
_NBA_PLAYOFF_RE = re.compile(
    r"\bnba\b.*(playoff|finals|conference\s+final)|"
    r"(playoff|finals|conference\s+final).*\bnba\b",
    re.I,
)
_NHL_PLAYOFF_RE = re.compile(
    r"\b(nhl\b|stanley).*(playoff|final)|playoff.*\b(nhl\b|stanley)\b",
    re.I,
)
_UFC_BOX_RE = re.compile(
    r"\bufc\b|mma|boxing|title\s+fight|heavyweight",
    re.I,
)
_TENNIS_TIER_RE = re.compile(
    r"\b(atp\s*500|wta\s*500|atp\s*1000|wta\s*1000|masters|hamburg|wimbledon|"
    r"roland\s+garros|us\s+open|australian\s+open)\b",
    re.I,
)
_BOOST_RE = re.compile(
    r"\b(playoff|play-off|semi-?final|quarter-?final|finals?|derby|"
    r"world\s+championship|major|lan\s+final|grand\s+final|matchday\s+38)\b",
    re.I,
)
_JUNK_RE = re.compile(
    r"\b(u21|youth|reserve|friendly|товарищ|women'?s\s+league|amateur)\b",
    re.I,
)
_MATCHUP_RE = re.compile(r"\s+vs\.?\s+|\s+—\s+|\s+–\s+", re.I)


def _blob(e: dict[str, Any]) -> str:
    return (
        f"{e.get('sport','')} {e.get('category','')} {e.get('title','')} "
        f"{e.get('subtitle','')} {e.get('league','')}"
    ).lower()


def category_bucket(e: dict[str, Any]) -> CategoryBucket:
    sport = detect_sport(e)
    if sport == "football":
        return "football"
    if sport == "hockey":
        return "hockey"
    if sport == "esports":
        return "esports"
    if sport == "formula1":
        return "formula1"
    if sport == "basketball":
        return "basketball"
    if sport in ("mma", "boxing", "ufc"):
        return "ufc"
    if sport == "tennis":
        return "tennis"
    return "other"


def emoji_for_sport(sport: str, e: dict[str, Any] | None = None) -> str:
    sp = str(sport or "").strip().lower()
    if sp in SPORT_EMOJI:
        return SPORT_EMOJI[sp]
    if e is not None:
        b = _blob(e)
        if _F1_RE.search(b):
            return SPORT_EMOJI["formula1"]
        if _ESPORTS_RE.search(b):
            return SPORT_EMOJI["esports"]
        if _HOCKEY_NHL_RE.search(b) or _HOCKEY_KHL_RE.search(b) or _HOCKEY_WORLD_RE.search(b):
            return SPORT_EMOJI["hockey"]
        if "nba" in b:
            return SPORT_EMOJI["basketball"]
        if _UFC_BOX_RE.search(b):
            return SPORT_EMOJI["mma"]
        if _FOOTBALL_LEAGUE_RE.search(b):
            return SPORT_EMOJI["football"]
    return "🏟"


def detect_sport(e: dict[str, Any]) -> str:
    sp = str(e.get("sport", "")).strip().lower()
    if sp in ("f1",):
        return "formula1"
    if sp and sp not in ("misc", "other", ""):
        return sp
    b = _blob(e)
    if _ESPORTS_RE.search(b) or _DOTA_CS_RE.search(b) or _DREAMLEAGUE_RE.search(b):
        return "esports"
    if _F1_RE.search(b):
        return "formula1"
    if _HOCKEY_NHL_RE.search(b) or _HOCKEY_KHL_RE.search(b) or _HOCKEY_WORLD_RE.search(b):
        return "hockey"
    if _NBA_PLAYOFF_RE.search(b) or ("nba" in b and "playoff" in b):
        return "basketball"
    if _UFC_BOX_RE.search(b):
        return "mma"
    if _TENNIS_TIER_RE.search(b):
        return "tennis"
    if _FOOTBALL_LEAGUE_RE.search(b):
        return "football"
    return sp or "other"


def is_khl_final(e: dict[str, Any]) -> bool:
    """KHL + Final/series/playoff или финал Ак Барс — Локомотив."""
    b = _blob(e)
    if not (_HOCKEY_KHL_RE.search(b) or "kontinental" in b):
        return False
    if re.search(
        r"\bfinal\b|финал|playoff|play-off|series\s*\d|semifinal|semi-final",
        b,
        re.I,
    ):
        return True
    if re.search(r"ak\s+bars", b, re.I) and re.search(r"lokomotiv", b, re.I):
        return True
    return False


def is_dreamleague_top_match(e: dict[str, Any]) -> bool:
    b = _blob(e)
    title = str(e.get("title", ""))
    is_dream = bool(
        _DREAMLEAGUE_RE.search(b) or re.search(r"dreamleague|dream\s+league", b, re.I)
    )
    if not is_dream:
        return False
    if _ESPORTS_TEAMS_RE.search(b) or _MATCHUP_RE.search(title):
        return True
    return bool(re.search(r"playoff|final|major|esl|blast|iem|dota", b, re.I))


def is_top_esports_match(e: dict[str, Any]) -> bool:
    b = _blob(e)
    title = str(e.get("title", ""))
    if is_dreamleague_top_match(e):
        return True
    if _ESPORTS_RE.search(b) and _ESPORTS_TEAMS_RE.search(b) and _MATCHUP_RE.search(title):
        return True
    if re.search(r"\b(iem|esl|blast|major|pgl|betboom)\b", b, re.I) and _MATCHUP_RE.search(title):
        return True
    return False


def is_atp_500_featured(e: dict[str, Any]) -> bool:
    b = _blob(e)
    title = str(e.get("title", ""))
    return bool(
        _TENNIS_TIER_RE.search(b)
        and _TENNIS_STARS_RE.search(b)
        and _MATCHUP_RE.search(title)
    )


def is_force_include_radar(e: dict[str, Any]) -> bool:
    """Всегда в финале, если есть в окне."""
    if is_khl_final(e):
        return True
    if is_dreamleague_top_match(e) or is_top_esports_match(e):
        return True
    if is_atp_500_featured(e):
        return True
    return False


def is_f1_core_session(e: dict[str, Any]) -> bool:
    return bool(_F1_CORE_SESSION_RE.search(_blob(e)))


def rule_watchability_tier(e: dict[str, Any]) -> WatchTier:
    if gastrobar_hard_reject(e):
        return "skip"
    sport = detect_sport(e)
    b = _blob(e)
    title = str(e.get("title", ""))

    if _JUNK_RE.search(b):
        return "skip"

    if sport == "formula1":
        if _F1_RE.search(b):
            return "high" if _F1_CORE_SESSION_RE.search(b) else "medium"
        return "medium"

    if sport == "hockey":
        if is_khl_final(e) or _HOCKEY_WORLD_RE.search(b):
            return "high"
        if _HOCKEY_NHL_RE.search(b) and re.search(r"playoff|final", b, re.I):
            return "high"
        if _HOCKEY_NATION_RE.search(b) and _MATCHUP_RE.search(title):
            return "high"
        if _MATCHUP_RE.search(title):
            return "medium"
        return "low"

    if sport == "esports":
        if is_dreamleague_top_match(e) or is_top_esports_match(e):
            return "high"
        if _ESPORTS_RE.search(b):
            if _MATCHUP_RE.search(title) or _ESPORTS_TEAMS_RE.search(b):
                return "high"
            return "medium"
        return "skip"

    if sport == "football":
        if not _MATCHUP_RE.search(title):
            return "skip"
        from football_watchability import is_eligible_football_league_now24

        item = {
            "league_id": e.get("league_id"),
            "league_country": e.get("league_country", ""),
            "league": e.get("league") or e.get("subtitle", ""),
            "title": title,
        }
        if is_eligible_football_league_now24(item):
            return "high"
        if _FOOTBALL_LEAGUE_RE.search(b):
            return "medium"
        return "low"

    if sport == "basketball":
        if _NBA_PLAYOFF_RE.search(b):
            return "high"
        if "nba" in b and _MATCHUP_RE.search(title):
            return "medium"
        return "skip"

    if sport in ("mma", "boxing", "ufc"):
        if _MATCHUP_RE.search(title) and _UFC_BOX_RE.search(b):
            return "high"
        return "skip"

    if sport == "tennis":
        if not _MATCHUP_RE.search(title):
            return "skip"
        if is_atp_500_featured(e):
            return "medium"
        if _TENNIS_TIER_RE.search(b):
            return "low"
        return "skip"

    return "skip"


def is_mandatory_radar_event(e: dict[str, Any]) -> bool:
    if is_force_include_radar(e):
        return True
    sport = detect_sport(e)
    b = _blob(e)
    if sport == "hockey" and is_khl_final(e):
        return True
    if sport == "esports" and is_top_esports_match(e):
        return True
    if sport == "football" and rule_watchability_tier(e) in ("high", "medium"):
        return True
    if sport == "basketball" and _NBA_PLAYOFF_RE.search(b):
        return True
    if sport == "formula1" and is_f1_core_session(e):
        return True
    return rule_watchability_tier(e) == "high"


def gastrobar_rank_score(e: dict[str, Any]) -> int:
    """Deterministic 0–100 для сортировки и guarantees."""
    tier = rule_watchability_tier(e)
    if tier == "skip":
        return 0

    base = {"high": 72, "medium": 52, "low": 34}[tier]
    b = _blob(e)
    score = base

    if _BOOST_RE.search(b):
        score += 10
    if category_bucket(e) == "formula1":
        if is_f1_core_session(e):
            score += 10
        elif re.search(r"practice|fp[123]", b, re.I):
            score += 3
        else:
            score += 6
    if category_bucket(e) == "hockey":
        if is_khl_final(e):
            score = max(score, 94)
        elif _HOCKEY_WORLD_RE.search(b):
            score += 18
        elif _HOCKEY_NHL_RE.search(b) and re.search(r"playoff", b, re.I):
            score += 12
        elif _HOCKEY_NATION_RE.search(b):
            score += 8
    if category_bucket(e) == "esports":
        if is_dreamleague_top_match(e):
            score = max(score, 92)
        elif is_top_esports_match(e):
            score = max(score, 85)
        elif re.search(r"major|dreamleague|playoff|final|iem|blast|esl", b, re.I):
            score += 16
        if _ESPORTS_TEAMS_RE.search(b):
            score += 10
    if category_bucket(e) == "tennis" and is_atp_500_featured(e):
        score = max(score, 58)
    if category_bucket(e) == "football":
        if re.search(r"champions\s+league|europa\s+league", b, re.I):
            score += 16
        if _FOOTBALL_TOP_CLUBS_RE.search(b):
            score += 10
        if "derby" in b:
            score += 10
    if category_bucket(e) == "basketball" and _NBA_PLAYOFF_RE.search(b):
        score += 14
    if category_bucket(e) == "ufc":
        score += 10

    if is_mandatory_radar_event(e):
        score = max(score, 68)

    return min(100, score)


def rule_priority_score(e: dict[str, Any]) -> int:
    return gastrobar_rank_score(e)


def radar_rules_drop_reason(
    e: dict[str, Any],
    *,
    for_now24: bool = False,
) -> str | None:
    """Только явный мусор (skip). Low/medium/high проходят в ranking."""
    if is_force_include_radar(e):
        return None
    tier = rule_watchability_tier(e)
    if tier == "skip":
        return "rules_skip"
    return None


def log_removed_by_rules(e: dict[str, Any], reason: str) -> None:
    log.info(
        "REMOVED_BY_RULES: title=%r category=%s sport=%s score=%s reason=%s local=%s",
        (e.get("title") or "")[:80],
        e.get("category"),
        e.get("sport"),
        gastrobar_rank_score(e),
        reason,
        e.get("local_datetime") or f"{e.get('local_date','')} {e.get('local_time','')}",
    )


def log_pool_scores(pool: list[dict[str, Any]], *, label: str = "pool") -> None:
    for e in pool:
        cat = category_bucket(e)
        sc = gastrobar_rank_score(e)
        tier = rule_watchability_tier(e)
        title = (e.get("title") or "")[:70]
        if cat == "esports":
            log.info(
                "%s ESPORTS_SCORE: %r score=%s tier=%s dream=%s top=%s",
                label,
                title,
                sc,
                tier,
                is_dreamleague_top_match(e),
                is_top_esports_match(e),
            )
        elif cat == "hockey":
            log.info(
                "%s KHL_SCORE: %r score=%s tier=%s khl_final=%s",
                label,
                title,
                sc,
                tier,
                is_khl_final(e),
            )
        elif cat == "tennis":
            log.info(
                "%s ATP_SCORE: %r score=%s tier=%s atp500_star=%s",
                label,
                title,
                sc,
                tier,
                is_atp_500_featured(e),
            )


def _event_rank_key(e: dict[str, Any]) -> tuple[int, int, str]:
    from next24 import resolve_event_local_datetime_vn

    dt = resolve_event_local_datetime_vn(e)
    ts = dt.isoformat() if dt else "9999"
    return (-gastrobar_rank_score(e), 0 if dt else 1, ts)


def _pool_has_category(pool: list[dict[str, Any]], cat: CategoryBucket) -> bool:
    return any(
        category_bucket(e) == cat and rule_watchability_tier(e) != "skip"
        for e in pool
    )


def _count_cat(picked: list[dict[str, Any]], cat: CategoryBucket) -> int:
    return sum(1 for x in picked if category_bucket(x) == cat)


def apply_category_guarantees(
    ranked: list[dict[str, Any]],
    window_pool: list[dict[str, Any]],
    *,
    mode: Literal["week", "now24", "next72"],
    max_items: int,
) -> list[dict[str, Any]]:
    """
    Force-include → category mins → fill by score с per-category cap (F1 не забивает всё).
    """
    mins = _NOW24_MIN_PER_CATEGORY if mode == "now24" else _WEEK_MIN_PER_CATEGORY
    caps = _NOW24_CAT_CAP if mode == "now24" else _WEEK_CAT_CAP
    if mode == "next72":
        mins = _WEEK_MIN_PER_CATEGORY
        caps = _WEEK_CAT_CAP
    eligible = [e for e in window_pool if radar_rules_drop_reason(e) is None]
    by_score = sorted(eligible, key=_event_rank_key)

    picked: list[dict[str, Any]] = []
    picked_ids: set[int] = set()

    def can_add(ev: dict[str, Any], *, force: bool = False) -> bool:
        if id(ev) in picked_ids:
            return False
        if force or is_force_include_radar(ev):
            return True
        cat = category_bucket(ev)
        if _count_cat(picked, cat) >= caps.get(cat, 99):
            return False
        return len(picked) < max_items

    def add(ev: dict[str, Any], *, force: bool = False) -> bool:
        if not can_add(ev, force=force):
            return False
        picked.append(ev)
        picked_ids.add(id(ev))
        return True

    for ev in sorted(
        [e for e in eligible if is_force_include_radar(e)],
        key=_event_rank_key,
    ):
        add(ev, force=True)

    for cat, min_n in mins.items():
        if min_n <= 0 or not _pool_has_category(window_pool, cat):
            continue
        cat_events = [e for e in by_score if category_bucket(e) == cat]
        have = _count_cat(picked, cat)
        for ev in cat_events:
            if have >= min_n:
                break
            if add(ev, force=True):
                have += 1

    for ev in by_score:
        if len(picked) >= max_items:
            break
        add(ev)

    from next24 import resolve_event_local_datetime_vn
    from zoneinfo import ZoneInfo

    vn = ZoneInfo("Asia/Ho_Chi_Minh")

    def chrono(ev: dict[str, Any]) -> datetime:
        dt = resolve_event_local_datetime_vn(ev)
        return dt if dt else datetime.max.replace(tzinfo=vn)

    return sorted(picked[:max_items], key=chrono)


def build_gastrobar_radar_output(
    in_window: list[dict[str, Any]],
    *,
    mode: Literal["week", "now24", "next72"],
    max_items: int,
    min_items: int = 0,
) -> list[dict[str, Any]]:
    """Rules layer: pass non-skip → rank → category guarantees → cap."""
    passed: list[dict[str, Any]] = []
    for e in in_window:
        if radar_rules_drop_reason(e, for_now24=(mode == "now24")) is None:
            ev = dict(e)
            sc = gastrobar_rank_score(ev)
            ev["radar_priority_score"] = sc
            ev["watchability_score"] = sc
            ev["radar_rules_tier"] = rule_watchability_tier(ev)
            passed.append(ev)

    out = apply_category_guarantees(passed, in_window, mode=mode, max_items=max_items)

    if min_items and len(out) < min_items and len(passed) >= min_items:
        out = apply_category_guarantees(
            passed, in_window, mode=mode, max_items=max(min_items, max_items)
        )

    from next24 import resolve_event_local_datetime_vn
    from zoneinfo import ZoneInfo

    vn = ZoneInfo("Asia/Ho_Chi_Minh")

    def chrono(ev: dict[str, Any]) -> datetime:
        dt = resolve_event_local_datetime_vn(ev)
        return dt if dt else datetime.max.replace(tzinfo=vn)

    return sorted(out, key=chrono)


def count_final_by_category(events: list[dict[str, Any]]) -> dict[str, int]:
    counts = {
        "FINAL_FOOTBALL": 0,
        "FINAL_HOCKEY": 0,
        "FINAL_ESPORTS": 0,
        "FINAL_F1": 0,
        "FINAL_NBA": 0,
        "FINAL_UFC": 0,
        "FINAL_TENNIS": 0,
    }
    for e in events:
        cat = category_bucket(e)
        if cat == "football":
            counts["FINAL_FOOTBALL"] += 1
        elif cat == "hockey":
            counts["FINAL_HOCKEY"] += 1
        elif cat == "esports":
            counts["FINAL_ESPORTS"] += 1
        elif cat == "formula1":
            counts["FINAL_F1"] += 1
        elif cat == "basketball":
            counts["FINAL_NBA"] += 1
        elif cat == "ufc":
            counts["FINAL_UFC"] += 1
        elif cat == "tennis":
            counts["FINAL_TENNIS"] += 1
    return counts


def check_rules_overfiltering(
    pool_size: int,
    after_rules: int,
    *,
    label: str = "radar",
) -> None:
    if pool_size > 100 and after_rules < 5:
        log.warning(
            "WARNING: rules_overfiltering_detected [%s] pool=%s after_rules=%s",
            label,
            pool_size,
            after_rules,
        )
