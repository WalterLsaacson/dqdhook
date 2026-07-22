"""Team-name aliases for Dongqiudi ↔ Polymarket fuzzy matching.

Maintain this table in the **match-bridge** skill. Keys are lowercase / Chinese
display forms (after light cleanup); values are canonical English tokens that
`normalize_team()` compares.

Add a row when a near-miss fails the 0.62 threshold because of abbreviations,
translations, or club-name variants (e.g. UCV ↔ Universidad Central…).
"""

from __future__ import annotations

# CN / alt spellings / abbreviations → English canonical tokens.
TEAM_ALIASES: dict[str, str] = {
    # --- K League / Allsvenskan (early bridge) ---
    "首尔": "seoul",
    "富川1995": "bucheon",
    "富川": "bucheon",
    "安养": "anyang",
    "光州": "gwangju",
    "哈马比": "hammarby",
    "代格福什": "degerfors",
    "埃尔夫斯堡": "elfsborg",
    "天狼星": "sirius",
    "哈尔姆斯塔德": "halmstad",
    "赫根": "hacken",
    "卡尔马": "kalmar",
    "马尔默": "malmo",
    "马尔莫": "malmo",
    "佐加顿斯": "djurgardens",
    "西班牙": "spain",
    "阿根廷": "argentina",
    "克卢日大学": "universitatea cluj",
    "康斯坦察灯塔": "farul",
    "克卢日": "universitatea cluj",
    "华盛顿精神": "washington spirit",
    "波士顿传奇": "boston legacy",
    "bucheon fc 1995": "bucheon",
    "bucheon 1995": "bucheon",
    "fc seoul": "seoul",
    "fc anyang": "anyang",
    "gwangju fc": "gwangju",
    "hammarby if": "hammarby",
    "degerfors if": "degerfors",
    "if elfsborg": "elfsborg",
    "ik sirius": "sirius",
    "halmstads bk": "halmstad",
    "bk hacken": "hacken",
    "malmo ff": "malmo",
    "kalmar ff": "kalmar",
    "orgryte is": "orgryte",
    "djurgardens if": "djurgardens",
    "fc universitatea cluj": "universitatea cluj",
    "fcv farul constanta": "farul",
    "boston legacy fc": "boston legacy",
    # --- Copa Sudamericana ---
    # DQD short name "UCV" vs PM "Universidad Central de Venezuela FC"
    "ucv": "universidad central venezuela",
    "universidad central venezuela": "universidad central venezuela",
    "universidad central de venezuela": "universidad central venezuela",
    "universidad central de venezuela fc": "universidad central venezuela",
}
