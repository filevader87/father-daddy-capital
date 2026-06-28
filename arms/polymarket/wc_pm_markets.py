"""
FDC World Cup Bot — Polymarket Market Discovery & Odds Parsing
===============================================================
Discovers World Cup match markets from PM Gamma REST API.
Parses match winner, over/under, spread, and prop markets into
structured market dicts with token IDs and current prices.

Gamma API: https://gamma-api.polymarket.com
"""

import json
import re
import urllib.request
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional

log = logging.getLogger("worldcup.pm")

GAMMA_BASE = "https://gamma-api.polymarket.com"
USER_AGENT = "FDC-WorldCup-Bot/1.0"

# ─── Market type classifiers ───
MARKET_TYPES = {
    "match_winner": [
        r"will.*win.*match",
        r"match.*winner",
        r"winner.*match",
        r"will.*win.*on.*20\d\d",  # "Will Brazil win on 2026-06-19?"
        r"will.*win.*\d{4}-\d{2}-\d{2}",
    ],
    "over_under": [
        r"over.*under",
        r"total.*goals",
        r"o/u",
        r"over.*goals",
        r"under.*goals",
    ],
    "spread": [
        r"spread",
        r"handicap",
        r"-\d+\.\d+",
        r"\+\d+\.\d+",
    ],
    "btts": [
        r"both.*teams.*score",
        r"btts",
    ],
    "correct_score": [
        r"correct.*score",
        r"exact.*score",
        r"score.*\d+-\d+",
    ],
    "to_advance": [
        r"advance",
        r"qualify",
        r"progress",
        r"reach.*round",
    ],
    "group_winner": [
        r"win.*group",
        r"group.*winner",
    ],
    "tournament_winner": [
        r"win.*world cup",
        r"world cup.*winner",
        r"win.*tournament",
    ],
}


def classify_market(question: str) -> str:
    """Classify a market by its question text."""
    q = question.lower()
    for mtype, patterns in MARKET_TYPES.items():
        for pat in patterns:
            if re.search(pat, q):
                return mtype
    return "other"


def gamma_get(url: str, timeout: int = 15) -> Optional[dict]:
    """GET request to Gamma API with error handling."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        log.debug(f"Gamma API error {url}: {e}")
        return None


def discover_worldcup_events(limit: int = 100, offset: int = 0) -> List[Dict]:
    """
    Discover World Cup events from PM Gamma API.
    Paginates through active events sorted by volume, filtering for WC-related.
    """
    events = []
    seen_ids = set()

    # Paginate through events sorted by volume
    page_offset = offset
    max_pages = 100  # Safety limit
    for page in range(max_pages):
        url = f"{GAMMA_BASE}/events?limit=100&offset={page_offset}&active=true&closed=false&order=volume&ascending=false"
        data = gamma_get(url, timeout=20)
        if not data:
            break
        for e in data:
            title = e.get("title", "").lower()
            slug = e.get("slug", "").lower()
            if ("world cup" in title or "world-cup" in slug or "fifa" in title) and "esports" not in title and "lol:" not in title:
                eid = e.get("id", "")
                if eid and eid not in seen_ids:
                    seen_ids.add(eid)
                    events.append(e)
        page_offset += 100
        if len(events) >= limit:
            break

    log.info(f"Discovered {len(events)} World Cup events")
    return events


def discover_match_events(limit: int = 200) -> List[Dict]:
    """
    Discover World Cup match-level events (not tournament futures).
    Match events have two teams playing in a specific game.
    """
    all_events = discover_worldcup_events(limit=limit)

    match_events = []
    for e in all_events:
        title = e.get("title", "")
        # Match events typically have "vs" or "v" in the title
        if re.search(r'\bvs?\b\.?\s', title, re.IGNORECASE):
            # Exclude outright winner events that happen to have "vs" in description
            if not re.search(r'winner|champion|golden boot|advance|qualify', title, re.IGNORECASE):
                match_events.append(e)
        # Also check for team vs team in slug
        slug = e.get("slug", "")
        if re.search(r'-vs-', slug) or re.search(r'-v-', slug):
            if e not in match_events:
                match_events.append(e)

    log.info(f"Found {len(match_events)} match-level events")
    return match_events


def parse_teams_from_event(event: Dict) -> Optional[tuple]:
    """
    Extract (home_team, away_team) from event title.
    Handles patterns like:
        "Mexico vs. Korea Republic - More Markets"
        "Belgium vs. IR Iran - More Markets"
        "Scotland vs. Morocco"
        "Canada vs. Qatar - Total Corners"
        "Côte d'Ivoire vs. Ecuador"
    Returns None if can't parse.
    """
    title = event.get("title", "")
    slug = event.get("slug", "")

    # Strip suffixes like "- More Markets", "- Total Corners", "- Exact Score", etc.
    clean_title = re.sub(r'\s*-\s*(More Markets|Total Corners|Exact Score|Player Props|.*Round.*|.*Stage.*)$', '', title)

    # Split on "vs" or "v." — use greedy match for away team to capture full name
    # Pattern: everything before " vs " is home, everything after is away
    vs_match = re.search(r'^(.+?)\s+vs\.?\s+(.+)$', clean_title, re.IGNORECASE)
    if vs_match:
        home = vs_match.group(1).strip()
        away = vs_match.group(2).strip()
        # Clean up trailing punctuation
        home = re.sub(r'[,\-]+$', '', home).strip()
        away = re.sub(r'[,\-]+$', '', away).strip()
        # Remove common prefixes
        for prefix in ['What will the announcers say during ']:
            if home.startswith(prefix):
                home = home[len(prefix):].strip()
        return (home, away)

    # Try slug: "fifwc-bel-irn-2026-06-21-more-markets"
    # Use FIFA 3-letter codes to resolve team names
    fifwc_match = re.search(r'fifwc-([a-z]+)-([a-z]+)-\d{4}', slug)
    if fifwc_match:
        home_code = fifwc_match.group(1).upper()
        away_code = fifwc_match.group(2).upper()
        home = FIFA_CODES.get(home_code, home_code)
        away = FIFA_CODES.get(away_code, away_code)
        return (home, away)

    return None


# FIFA 3-letter country codes → full team names
FIFA_CODES = {
    "ARG": "Argentina", "AUS": "Australia", "AUT": "Austria",
    "BEL": "Belgium", "BRA": "Brazil", "CAN": "Canada",
    "CHI": "Chile", "COL": "Colombia", "CRC": "Costa Rica",
    "CRO": "Croatia", "CZE": "Czechia", "DEN": "Denmark",
    "ECU": "Ecuador", "EGY": "Egypt", "ENG": "England",
    "ESP": "Spain", "FRA": "France", "GER": "Germany",
    "GHA": "Ghana", "GRE": "Greece", "HAI": "Haiti",
    "HON": "Honduras", "IRN": "Iran", "IRQ": "Iraq",
    "ISL": "Iceland", "ISR": "Israel", "ITA": "Italy",
    "JAM": "Jamaica", "JPN": "Japan", "KOR": "South Korea",
    "MAR": "Morocco", "MEX": "Mexico", "NED": "Netherlands",
    "NOR": "Norway", "NZL": "New Zealand", "PAN": "Panama",
    "PAR": "Paraguay", "PER": "Peru", "POR": "Portugal",
    "QAT": "Qatar", "ROU": "Romania", "SCO": "Scotland",
    "SEN": "Senegal", "SRB": "Serbia", "SUI": "Switzerland",
    "SWE": "Sweden", "TUN": "Tunisia", "TUR": "Türkiye",
    "UKR": "Ukraine", "URU": "Uruguay", "USA": "USA",
    "UZB": "Uzbekistan", "WAL": "Wales",
    "ALG": "Algeria", "BHR": "Bahrain", "BFA": "Burkina Faso",
    "BIH": "Bosnia", "BOL": "Bolivia", "CPV": "Cape Verde",
    "CIV": "Côte d'Ivoire", "CMR": "Cameroon", "COD": "DR Congo",
    "CV": "Cape Verde", "CUR": "Curaçao",
    "GAB": "Gabon", "GAM": "Gambia", "GEO": "Georgia",
    "HUN": "Hungary", "IRL": "Ireland", "JOR": "Jordan",
    "KSA": "Saudi Arabia", "KVX": "Kosovo",
    "LUX": "Luxembourg", "MLI": "Mali", "MTN": "Montenegro",
    "NGA": "Nigeria", "PRK": "North Korea", "PLE": "Palestine",
    "POL": "Poland", "RSA": "South Africa", "RUS": "Russia",
    "SLO": "Slovenia", "SVK": "Slovakia", "SYR": "Syria",
    "THA": "Thailand", "TPE": "Chinese Taipei", "UAE": "United Arab Emirates",
    "VIE": "Vietnam", "ZIM": "Zimbabwe",
}


def parse_market_prices(market: Dict) -> Optional[tuple]:
    """
    Parse YES/NO prices from a market dict.
    Returns (yes_price, no_price) or None.
    """
    prices_raw = market.get("outcomePrices", "[]")
    if isinstance(prices_raw, str):
        try:
            prices = json.loads(prices_raw)
        except json.JSONDecodeError:
            return None
    else:
        prices = prices_raw

    if not prices or len(prices) < 2:
        return None

    try:
        yes_price = float(prices[0])
        no_price = float(prices[1])
        return (yes_price, no_price)
    except (ValueError, TypeError):
        return None


def parse_token_ids(market: Dict) -> Optional[tuple]:
    """Parse CLOB token IDs from a market dict. Returns (yes_token, no_token) or None."""
    token_ids_raw = market.get("clobTokenIds", "[]")
    if isinstance(token_ids_raw, str):
        try:
            token_ids = json.loads(token_ids_raw)
        except json.JSONDecodeError:
            return None
    else:
        token_ids = token_ids_raw

    if not token_ids or len(token_ids) < 2:
        return None

    return (token_ids[0], token_ids[1])


def parse_match_markets(event: Dict) -> List[Dict]:
    """
    Parse all markets within a match event into structured dicts.
    Each market dict has:
        - question, market_type, yes_price, no_price
        - yes_token_id, no_token_id, condition_id
        - volume, liquidity, neg_risk
    """
    markets = event.get("markets", [])
    parsed = []
    event_teams = parse_teams_from_event(event)

    for m in markets:
        question = m.get("question", "")
        prices = parse_market_prices(m)
        tokens = parse_token_ids(m)
        if not prices:
            continue

        yes_price, no_price = prices
        mtype = classify_market(question)
        q_lower = question.lower()

        # Extract over/under line — try multiple patterns
        ou_line = None
        ou_match = re.search(r'o/u\s+(\d+\.?\d*)', q_lower)
        if ou_match:
            ou_line = float(ou_match.group(1))
        else:
            ou_match = re.search(r'(\d+\.?\d*)\s*goals', q_lower)
            if ou_match and mtype == "over_under":
                ou_line = float(ou_match.group(1))
            else:
                ou_match = re.search(r'over.*?(\d+\.?\d*)|under.*?(\d+\.?\d*)', q_lower)
                if ou_match:
                    ou_line = float(ou_match.group(1) or ou_match.group(2))

        # Extract team-specific O/U
        team_ou = None
        is_half = False
        if mtype == "over_under":
            # Check for 1st half / 2nd half
            if "1st half" in q_lower or "first half" in q_lower:
                is_half = True
            # Try to extract team name from "Team O/U X.X" pattern
            # PM format: "Germany vs. Côte d'Ivoire: Côte d'Ivoire 1st Half O/U 0.5"
            # or "Paraguay vs. Australia: O/U 1.5" (total goals)
            colon_split = question.split(":")
            if len(colon_split) > 1:
                after_colon = colon_split[1].strip()
                # Check if a team name appears after the colon
                for t in event_teams or []:
                    if t.lower() in after_colon.lower():
                        team_ou = t
                        break

        # Extract spread line if present
        spread_line = None
        spread_match = re.search(r'(-?\d+\.?\d*)', question)
        if mtype == "spread" and spread_match:
            spread_line = float(spread_match.group(1))

        # Extract team name from match winner question
        winner_team = None
        if mtype == "match_winner":
            if event_teams:
                for t in event_teams:
                    if t.lower() in question.lower():
                        winner_team = t
                        break

        market_dict = {
            "question": question,
            "market_type": mtype,
            "yes_price": round(yes_price, 4),
            "no_price": round(no_price, 4),
            "yes_token_id": tokens[0] if tokens else "",
            "no_token_id": tokens[1] if tokens else "",
            "condition_id": m.get("conditionId", m.get("condition_id", "")),
            "market_id": m.get("id", ""),
            "volume": float(m.get("volume", 0) or 0),
            "liquidity": float(m.get("liquidity", 0) or 0),
            "neg_risk": True,
            "slug": event.get("slug", ""),
            "ou_line": ou_line,
            "team_ou": team_ou,
            "is_half": is_half,
            "spread_line": spread_line,
            "winner_team": winner_team,
            "event_title": event.get("title", ""),
        }
        parsed.append(market_dict)

    return parsed


def discover_all_worldcup_markets(limit: int = 200) -> List[Dict]:
    """
    Full discovery pipeline: find all WC match markets.
    Uses TWO approaches:
    1. /events endpoint — main match events (W/D/L markets with high volume)
    2. /markets endpoint — "More Markets" events (O/U, BTTS, corners, etc.)

    Returns list of dicts, each with:
        - event: event metadata
        - teams: (home, away) or None
        - markets: list of parsed market dicts
    """
    results = []
    seen_event_ids = set()

    # ─── Approach 1: /events endpoint for main match events ───
    # These have W/D/L markets: "Will X win?", "Will Y win?", "Will X vs Y end in a draw?"
    page_offset = 0
    for page in range(200):
        url = f"{GAMMA_BASE}/events?limit=100&offset={page_offset}&active=true&closed=false&order=volume&ascending=false"
        data = gamma_get(url, timeout=20)
        if not data:
            break
        for e in data:
            slug = e.get("slug", "").lower()
            title = e.get("title", "")
            if "fifwc" not in slug:
                continue
            if "more-markets" in slug or "total-corners" in slug or "player-props" in slug or "exact-score" in slug:
                continue  # These are handled by approach 2
            if "announcer" in title.lower() or "esports" in title.lower():
                continue

            eid = e.get("id", "")
            if eid in seen_event_ids:
                continue
            seen_event_ids.add(eid)

            teams = parse_teams_from_event(e)
            markets = parse_match_markets(e)

            # Filter to tradable types
            tradable_types = {"match_winner", "over_under", "spread", "btts", "correct_score", "draw"}
            tradable = [m for m in markets if m["market_type"] in tradable_types]
            if not tradable:
                continue

            results.append({
                "event": {
                    "id": e.get("id", ""),
                    "title": e.get("title", ""),
                    "slug": e.get("slug", ""),
                    "volume": float(e.get("volume", 0) or 0),
                    "liquidity": float(e.get("liquidity", 0) or 0),
                    "startDate": e.get("startDate", ""),
                    "endDate": e.get("endDate", ""),
                },
                "teams": teams,
                "markets": tradable,
            })

        page_offset += 100

    # ─── Approach 2: /markets endpoint for More Markets (O/U, BTTS, etc.) ───
    wc_teams = [
        "Belgium", "Iran", "Brazil", "Haiti", "Canada", "Qatar", "Mexico",
        "Korea", "Argentina", "France", "Spain", "England", "Germany",
        "Portugal", "USA", "Japan", "Morocco", "Italy", "Netherlands",
        "Australia", "Croatia", "Uruguay", "Denmark", "Switzerland",
        "Colombia", "Ecuador", "Senegal", "Serbia", "Poland", "Ukraine",
        "Austria", "Czech", "Sweden", "Norway", "Turkey", "Türkiye",
        "Costa Rica", "Paraguay", "Peru", "Chile", "Panama", "Jamaica",
        "Honduras", "New Zealand", "Egypt", "Nigeria", "Tunisia", "Ghana",
        "Cameroon", "Ivory Coast", "Côte d'Ivoire", "Saudi Arabia",
        "South Africa", "DR Congo", "Mali", "Algeria", "Wales", "Scotland",
        "Hungary", "Romania", "Greece", "Slovenia", "Slovakia", "Ireland",
        "Finland", "Iceland", "Bosnia", "Albania", "Georgia", "Israel",
        "Cape Verde", "Cabo Verde", "Jordan", "Iraq", "Uzbekistan",
        "Bahrain", "Syria", "Thailand", "Vietnam", "Indonesia",
        "United States", "South Korea", "Korea Republic",
    ]

    all_more_markets = []
    offset = 0
    for page in range(200):
        url = f"{GAMMA_BASE}/markets?limit=100&offset={offset}&active=true&closed=false&order=volume&ascending=false"
        data = gamma_get(url, timeout=20)
        if not data:
            break
        for m in data:
            q = m.get("question", "")
            if " vs " not in q and " vs. " not in q:
                continue
            if not any(team in q for team in wc_teams):
                continue
            events = m.get("events", [])
            is_wc = False
            for ev in events:
                slug = ev.get("slug", "").lower()
                if "fifwc" in slug:
                    is_wc = True
                    break
            if not is_wc:
                continue
            if "announcer" in q.lower():
                continue
            if "win the 2026 fifa world cup" in q.lower():
                continue
            all_more_markets.append(m)
        offset += 100
        if len(all_more_markets) >= limit:
            break

    # Group More Markets by event
    events_map = {}
    for m in all_more_markets:
        for ev in m.get("events", []):
            eid = ev.get("id", "")
            slug = ev.get("slug", "").lower()
            if "fifwc" in slug and eid not in seen_event_ids:
                if eid not in events_map:
                    events_map[eid] = {"event": ev, "markets": []}
                events_map[eid]["markets"].append(m)

    for info in events_map.values():
        ev = info["event"]
        markets_raw = info["markets"]
        teams = parse_teams_from_event(ev)
        parsed = []
        for m in markets_raw:
            market_dict = _parse_single_market(m, ev)
            if market_dict:
                parsed.append(market_dict)
        tradable_types = {"match_winner", "over_under", "spread", "btts", "correct_score", "draw"}
        tradable = [m for m in parsed if m["market_type"] in tradable_types]
        if not tradable:
            continue
        eid = ev.get("id", "")
        if eid in seen_event_ids:
            # Merge into existing result
            for r in results:
                if r["event"]["id"] == eid:
                    r["markets"].extend(tradable)
                    break
        else:
            seen_event_ids.add(eid)
            results.append({
                "event": {
                    "id": ev.get("id", ""),
                    "title": ev.get("title", ""),
                    "slug": ev.get("slug", ""),
                    "volume": float(ev.get("volume", 0) or 0),
                    "liquidity": float(ev.get("liquidity", 0) or 0),
                    "startDate": ev.get("startDate", ""),
                    "endDate": ev.get("endDate", ""),
                },
                "teams": teams,
                "markets": tradable,
            })

    log.info(f"Full discovery: {len(results)} matches with tradable markets")
    return results


def _parse_single_market(m: Dict, event: Dict) -> Optional[Dict]:
    """Parse a single market dict from /markets endpoint."""
    question = m.get("question", "")
    prices = parse_market_prices(m)
    tokens = parse_token_ids(m)
    if not prices:
        return None

    yes_price, no_price = prices

    # Classify market type — expanded for WC market patterns
    q_lower = question.lower()
    mtype = "other"

    # Draw market: "Will X vs Y end in a draw?"
    if "end in a draw" in q_lower or "finish in a draw" in q_lower:
        mtype = "draw"
    # Over/Under
    elif re.search(r"o/u\s+\d", q_lower) or re.search(r"over.*under.*\d", q_lower):
        # Exclude corners, cards, fouls, offsides, saves, shots, etc.
        if any(x in q_lower for x in ["corner", "card", "foul", "offside", "save", "shot",
                                      "yellow", "red", "penalty", "throw", "cross",
                                      "possession", "pass", "tackle", "interception"]):
            mtype = "other"
        else:
            mtype = "over_under"
    # Both teams to score
    elif "both teams to score" in q_lower or "btts" in q_lower:
        mtype = "btts"
    # Correct score
    elif re.search(r"exact.*score.*\d+-\d+", q_lower) or re.search(r"score.*\d+\s*-\s*\d+", q_lower):
        mtype = "correct_score"
    # Match winner
    elif re.search(r"will.*win.*match", q_lower) or "match winner" in q_lower:
        mtype = "match_winner"
    # Spread/handicap
    elif re.search(r"-\d+\.5|\+\d+\.5|spread|handicap", q_lower):
        mtype = "spread"

    # Extract O/U line
    ou_line = None
    ou_match = re.search(r"o/u\s+(\d+\.?\d*)", q_lower)
    if ou_match:
        ou_line = float(ou_match.group(1))
    else:
        ou_match = re.search(r"over.*?(\d+\.?\d*)|under.*?(\d+\.?\d*)", q_lower)
        if ou_match:
            ou_line = float(ou_match.group(1) or ou_match.group(2))

    # Extract team-specific O/U
    team_ou = None
    is_half = False
    if mtype == "over_under":
        # Check for 1st half / 2nd half
        if "1st half" in q_lower or "first half" in q_lower:
            is_half = True
        # Extract team name from question
        # PM format: "Germany vs. Côte d'Ivoire: Côte d'Ivoire 1st Half O/U 0.5"
        colon_split = question.split(":")
        if len(colon_split) > 1:
            after_colon = colon_split[1].strip()
            # Get event teams
            ev_teams = parse_teams_from_event(event)
            if ev_teams:
                for t in ev_teams:
                    if t.lower() in after_colon.lower():
                        team_ou = t
                        break

    # Extract correct score
    score = None
    score_match = re.search(r"(\d+)\s*-\s*(\d+)", question)
    if score_match and mtype == "correct_score":
        score = f"{score_match.group(1)}-{score_match.group(2)}"

    # Extract winner team from draw question
    winner_team = None
    if mtype == "draw":
        # "Will Scotland vs. Morocco end in a draw?" — YES = draw, NO = not draw
        pass  # Handled specially in edge computation

    return {
        "question": question,
        "market_type": mtype,
        "yes_price": round(yes_price, 4),
        "no_price": round(no_price, 4),
        "yes_token_id": tokens[0] if tokens else "",
        "no_token_id": tokens[1] if tokens else "",
        "condition_id": m.get("conditionId", m.get("condition_id", "")),
        "market_id": m.get("id", ""),
        "volume": float(m.get("volume", 0) or 0),
        "liquidity": float(m.get("liquidity", 0) or 0),
        "neg_risk": True,
        "slug": event.get("slug", ""),
        "ou_line": ou_line,
        "team_ou": team_ou,
        "is_half": is_half,
        "spread_line": None,
        "winner_team": winner_team,
        "event_title": event.get("title", ""),
        "score": score,
    }


def discover_tournament_markets(limit: int = 100) -> List[Dict]:
    """
    Discover tournament-level markets (outright winner, golden boot, etc.)
    Returns list of parsed market dicts.
    """
    all_events = discover_worldcup_events(limit=limit)

    markets = []
    for event in all_events:
        title = event.get("title", "")
        # Tournament futures — not match-level
        if re.search(r'winner|champion|golden boot|advance|qualify|group', title, re.IGNORECASE):
            parsed = parse_match_markets(event)
            markets.extend(parsed)

    log.info(f"Discovered {len(markets)} tournament-level markets")
    return markets