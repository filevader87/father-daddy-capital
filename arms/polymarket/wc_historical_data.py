"""
FIFA World Cup Historical Backtest Database
=============================================
Actual results from WC 2018 (Russia) and WC 2022 (Qatar) group stage.
Used for backtesting the Elo+Poisson prediction model.

Each entry: (home_team, away_team, home_goals, away_goals, stage, year)
"""

# WC 2022 (Qatar) — Group Stage results
# 48 group stage matches
WC_2022_GROUP_STAGE = [
    # Group A
    ("Senegal", "Netherlands", 0, 2),
    ("Qatar", "Ecuador", 0, 2),
    ("Qatar", "Senegal", 1, 3),
    ("Netherlands", "Ecuador", 1, 1),
    ("Ecuador", "Senegal", 1, 2),
    ("Netherlands", "Qatar", 2, 0),
    # Group B
    ("England", "Iran", 6, 2),
    ("USA", "Wales", 1, 1),
    ("Wales", "Iran", 0, 2),
    ("England", "USA", 0, 0),
    ("Wales", "England", 0, 3),
    ("Iran", "USA", 0, 1),
    # Group C
    ("Argentina", "Saudi Arabia", 1, 2),
    ("Mexico", "Poland", 0, 0),
    ("Poland", "Saudi Arabia", 2, 0),
    ("Argentina", "Mexico", 2, 0),
    ("Poland", "Argentina", 0, 2),
    ("Saudi Arabia", "Mexico", 1, 2),
    # Group D
    ("Denmark", "Tunisia", 0, 0),
    ("France", "Australia", 4, 1),
    ("Tunisia", "Australia", 0, 1),
    ("France", "Denmark", 2, 1),
    ("Australia", "Denmark", 1, 0),
    ("Tunisia", "France", 1, 0),
    # Group E
    ("Germany", "Japan", 1, 2),
    ("Spain", "Costa Rica", 7, 0),
    ("Japan", "Costa Rica", 0, 1),
    ("Spain", "Germany", 1, 1),
    ("Japan", "Spain", 2, 1),
    ("Costa Rica", "Germany", 2, 4),
    # Group F
    ("Morocco", "Croatia", 0, 0),
    ("Belgium", "Canada", 1, 0),
    ("Belgium", "Morocco", 0, 2),
    ("Croatia", "Canada", 4, 1),
    ("Croatia", "Belgium", 0, 0),
    ("Canada", "Morocco", 1, 2),
    # Group G
    ("Switzerland", "Cameroon", 1, 0),
    ("Brazil", "Serbia", 2, 0),
    ("Cameroon", "Serbia", 3, 3),
    ("Brazil", "Switzerland", 1, 0),
    ("Serbia", "Switzerland", 2, 3),
    ("Cameroon", "Brazil", 1, 0),
    # Group H
    ("Uruguay", "South Korea", 0, 0),
    ("Portugal", "Ghana", 3, 2),
    ("South Korea", "Ghana", 2, 3),
    ("Portugal", "Uruguay", 2, 0),
    ("Ghana", "Uruguay", 0, 2),
    ("South Korea", "Portugal", 2, 1),
]

# WC 2018 (Russia) — Group Stage results
WC_2018_GROUP_STAGE = [
    # Group A
    ("Russia", "Saudi Arabia", 5, 0),
    ("Egypt", "Uruguay", 0, 1),
    ("Russia", "Egypt", 3, 1),
    ("Uruguay", "Saudi Arabia", 1, 0),
    ("Uruguay", "Russia", 3, 0),
    ("Saudi Arabia", "Egypt", 2, 1),
    # Group B
    ("Morocco", "Iran", 0, 1),
    ("Portugal", "Spain", 3, 3),
    ("Portugal", "Morocco", 1, 0),
    ("Iran", "Spain", 0, 1),
    ("Iran", "Portugal", 1, 1),
    ("Spain", "Morocco", 2, 2),
    # Group C
    ("France", "Australia", 2, 1),
    ("Peru", "Denmark", 0, 1),
    ("Denmark", "Australia", 1, 1),
    ("France", "Peru", 1, 0),
    ("Denmark", "France", 0, 0),
    ("Australia", "Peru", 0, 2),
    # Group D
    ("Argentina", "Iceland", 1, 1),
    ("Croatia", "Nigeria", 2, 0),
    ("Argentina", "Croatia", 0, 3),
    ("Nigeria", "Iceland", 2, 0),
    ("Nigeria", "Argentina", 1, 2),
    ("Iceland", "Croatia", 1, 2),
    # Group E
    ("Costa Rica", "Serbia", 0, 1),
    ("Brazil", "Switzerland", 1, 1),
    ("Brazil", "Costa Rica", 2, 0),
    ("Serbia", "Switzerland", 1, 2),
    ("Serbia", "Brazil", 0, 2),
    ("Switzerland", "Costa Rica", 2, 2),
    # Group F
    ("Germany", "Mexico", 0, 1),
    ("Sweden", "South Korea", 1, 0),
    ("South Korea", "Mexico", 1, 2),
    ("Germany", "Sweden", 2, 1),
    ("South Korea", "Germany", 2, 0),
    ("Mexico", "Sweden", 0, 3),
    # Group G
    ("Belgium", "Panama", 3, 0),
    ("Tunisia", "England", 1, 2),
    ("Belgium", "Tunisia", 5, 2),
    ("England", "Panama", 6, 1),
    ("England", "Belgium", 0, 1),
    ("Panama", "Tunisia", 1, 2),
    # Group H
    ("Colombia", "Japan", 1, 2),
    ("Poland", "Senegal", 1, 2),
    ("Japan", "Senegal", 2, 2),
    ("Poland", "Colombia", 0, 3),
    ("Japan", "Poland", 0, 1),
    ("Senegal", "Colombia", 0, 1),
]

def get_historical_matches():
    """Return all historical WC group stage matches."""
    matches = []
    for m in WC_2022_GROUP_STAGE:
        matches.append((*m, "group", 2022))
    for m in WC_2018_GROUP_STAGE:
        matches.append((*m, "group", 2018))
    return matches