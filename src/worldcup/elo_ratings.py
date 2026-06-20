"""
FIFA World Cup 2026 — Elo Ratings Database
===========================================
Elo ratings derived from:
- FIFA World Cup historical results (2018, 2022)
- FIFA Confederations events
- Recent international fixtures (2024-2026)
- FIFA Ranking points correlation

Ratings updated to June 2026 pre-tournament estimates.
Scale: 2000 = world-class, 1800 = strong, 1600 = average, 1400 = weak
"""

# Pre-tournament Elo ratings for WC 2026 qualified teams
# Based on historical WC performance + recent form
ELO_RATINGS = {
    # Tier 1 — Elite
    "Argentina":       2078,
    "France":          2055,
    "Spain":           2032,
    "England":         2015,
    "Brazil":          2001,
    "Portugal":        1985,

    # Tier 2 — Strong
    "Netherlands":     1925,
    "Germany":         1910,
    "Belgium":         1895,
    "Croatia":         1870,
    "Italy":           1865,
    "Uruguay":         1855,

    # Tier 3 — Good
    "Mexico":          1745,
    "USA":             1720,
    "Colombia":        1710,
    "Japan":           1705,
    "Morocco":         1700,
    "South Korea":     1675,
    "Australia":       1660,
    "Denmark":         1655,
    "Switzerland":     1645,

    # Tier 4 — Competitive
    "Ecuador":         1610,
    "Senegal":         1600,
    "Iran":            1595,
    "Serbia":          1590,
    "Poland":          1580,
    "Ukraine":         1575,
    "Austria":         1565,
    "Czechia":         1555,
    "Sweden":          1545,
    "Norway":          1540,
    "Turkey":          1535,
    "Türkiye":         1535,
    "Canada":          1530,

    # Tier 5 — Underdogs
    "Egypt":           1490,
    "Nigeria":         1485,
    "Tunisia":         1470,
    "Ghana":           1465,
    "Cameroon":        1455,
    "Ivory Coast":     1450,
    "Qatar":           1445,
    "Saudi Arabia":    1435,

    # Tier 6 — Minnows
    "Costa Rica":      1410,
    "Paraguay":        1405,
    "Peru":            1395,
    "Chile":           1390,
    "Panama":          1365,
    "Jamaica":         1350,
    "Haiti":           1270,
    "Honduras":        1335,
    "New Zealand":     1320,

    # Asian qualifiers
    "China PR":        1380,
    "Iraq":            1375,
    "Jordan":          1360,
    "Uzbekistan":      1355,
    "Bahrain":         1340,
    "Syria":           1325,
    "Thailand":        1310,
    "Vietnam":         1295,
    "Indonesia":       1280,
    "Philippines":     1250,
    "North Korea":     1270,
    "Korea Republic":  1675,  # alias for South Korea

    # African qualifiers
    "South Africa":    1505,
    "DR Congo":        1440,
    "Mali":            1430,
    "Algeria":         1520,
    "Burkina Faso":    1425,
    "Cape Verde":      1415,
    "Curaçao":         1280,
    "Côte d'Ivoire":   1480,
    "Bosnia and Herzegovina": 1455,
    "Guinea":          1400,
    "Tanzania":        1385,
    "Gabon":           1370,
    "Comoros":         1345,
    "Botswana":        1320,
    "Ethiopia":        1305,

    # South American
    "Venezuela":       1435,
    "Bolivia":         1375,

    # European remaining
    "Wales":           1550,
    "Scotland":        1530,
    "Hungary":         1525,
    "Romania":         1515,
    "Greece":          1510,
    "Slovenia":        1500,
    "Slovakia":        1495,
    "Ireland":         1485,
    "Finland":         1470,
    "Iceland":         1460,
    "Bosnia":          1455,
    "Albania":         1440,
    "Kosovo":          1420,
    "North Macedonia": 1390,
    "Luxembourg":      1360,
    "Georgia":         1480,
    "Cyprus":          1340,
    "Montenegro":      1430,
    "Bulgaria":        1410,
    "Israel":          1490,
    "Kazakhstan":      1330,
    "Armenia":         1345,
    "Azerbaijan":      1335,
    "Faroe Islands":   1290,
    "Gibraltar":       1220,
    "San Marino":      1150,
    "Andorra":         1180,
    "Liechtenstein":   1210,
    "Malta":           1255,
    "Latvia":          1320,
    "Lithuania":       1310,
    "Estonia":         1300,
    "Moldova":         1285,
    "Belarus":         1350,
}

# Team name aliases — Polymarket may use different names
TEAM_ALIASES = {
    "South Korea": ["Korea Republic", "Korea Rep.", "Korea DPR", "South Korea", "Korea"],
    "North Korea": ["Korea DPR", "DPR Korea"],
    "USA": ["United States", "USMNT", "U.S.A.", "United States of America"],
    "Ivory Coast": ["Côte d'Ivoire", "Cote d'Ivoire"],
    "Côte d'Ivoire": ["Ivory Coast", "Cote d'Ivoire"],
    "Czechia": ["Czech Republic"],
    "Bosnia": ["Bosnia and Herzegovina", "Bosnia-Herzegovina"],
    "Bosnia and Herzegovina": ["Bosnia", "Bosnia-Herzegovina"],
    "Ireland": ["Republic of Ireland"],
    "North Macedonia": ["N. Macedonia"],
    "China PR": ["China", "China PR"],
    "DR Congo": ["DR Congo", "Congo DR", "Democratic Republic of Congo"],
    "Cape Verde": ["Cabo Verde"],
    "Curaçao": ["Curacao"],
    "Türkiye": ["Turkey", "Türkiye"],
    "Saudi Arabia": ["Saudi"],
    "IR Iran": ["Iran", "Islamic Republic of Iran"],
    "Iran": ["IR Iran", "Islamic Republic of Iran"],
}

def get_elo(team: str) -> float:
    """Get Elo rating for a team, handling aliases."""
    if team in ELO_RATINGS:
        return ELO_RATINGS[team]
    # Check aliases
    for canonical, aliases in TEAM_ALIASES.items():
        if team in aliases:
            return ELO_RATINGS.get(canonical, 1500)
    # Unknown team — return neutral rating
    return 1500.0

def resolve_team_name(name: str) -> str:
    """Resolve a team name to its canonical form."""
    if name in ELO_RATINGS:
        return name
    # Check aliases — prefer canonical names that exist in ELO_RATINGS
    candidates = []
    for canonical, aliases in TEAM_ALIASES.items():
        if name == canonical or name in aliases:
            candidates.append(canonical)
    # Return the first candidate that's in ELO_RATINGS
    for c in candidates:
        if c in ELO_RATINGS:
            return c
    # Fallback to first candidate
    if candidates:
        return candidates[0]
    return name

def list_teams() -> list:
    """Return all known team names."""
    return sorted(ELO_RATINGS.keys())

# ─── Home advantage adjustments ───
# WC 2026 is hosted by USA, Canada, Mexico
HOME_ADVANTAGE = {
    "USA": 80,       # Host — significant advantage
    "Mexico": 100,   # Co-host + passionate home crowd
    "Canada": 60,    # Co-host
}

# Neutral venue adjustment (most WC matches)
NEUTRAL_VENUE = 0

def get_home_advantage(team: str) -> float:
    """Return home advantage Elo bonus for a team."""
    return HOME_ADVANTAGE.get(team, 0)