"""Reviewed rules for the 50-region board used by the local transcribed pack."""

from .definitions import PER_UNIT_SITE
from .model import TURN_ORDER

PRINTED_REGIONS = frozenset("""
    canada united-states latin-america united-kingdom scandinavia western-europe germany
    italy eastern-europe balkans ukraine moscow russia siberia vladivostok kazakhstan
    mongolia china-west china-east japan iwo-jima southeast-asia philippines indonesia
    new-guinea india middle-east north-africa south-africa madagascar australia new-zealand
    hawaii north-atlantic southeast-pacific central-atlantic south-atlantic north-sea
    baltic-sea black-sea caspian-sea mediterranean arabian-sea indian-ocean south-china-sea
    east-china-sea north-pacific central-pacific east-pacific south-pacific
""".split())


def apply_half_sites(regions, sites, initial):
    for region in ("scandinavia", "north-africa", "hawaii"):
        site = next((site for site in sites if site["region"] == region and not site.get("dynamic")), None)
        if site is None:
            site = {"id": "score-" + region, "region": region, "countries": list(TURN_ORDER),
                    "requires_supply": True}
            sites.append(site)
        site.update(vp=1, scoring=PER_UNIT_SITE)

    hawaii = next(row for row in regions if row["id"] == "hawaii")
    hawaii["construction_requires_adjacent_for"] = ["united-states"]
    pacific_army = next(token for token in initial if token["token_id"] == "united-states-army-2")
    pacific_army["position_id"] = "hawaii"


def edge_pairs(rows):
    return {tuple(sorted(row)) for row in rows}


# Shared borders, including the printed links across the east/west edge.
ADDED = edge_pairs((
    ("north-sea", "north-africa"),
    ("north-sea", "central-atlantic"),
    ("south-atlantic", "indian-ocean"),
    ("south-atlantic", "southeast-pacific"),
    ("canada", "east-pacific"),
    ("united-states", "southeast-pacific"),
    ("latin-america", "east-pacific"),
    ("russia", "baltic-sea"),
    ("ukraine", "caspian-sea"),
    ("madagascar", "arabian-sea"),
    ("china-west", "southeast-asia"),
    ("china-east", "vladivostok"),
    ("australia", "south-china-sea"),
    ("south-china-sea", "central-pacific"),
    ("hawaii", "east-pacific"),
    ("new-zealand", "east-pacific"),
))
REMOVED = edge_pairs((
    ("united-kingdom", "north-atlantic"),
    ("germany", "north-sea"),
    ("western-europe", "north-atlantic"),
    ("north-africa", "north-atlantic"),
    ("north-africa", "central-atlantic"),
    ("united-states", "north-pacific"),
    ("siberia", "north-pacific"),
    ("japan", "north-pacific"),
    ("india", "indian-ocean"),
    ("south-africa", "madagascar"),
    ("south-pacific", "southeast-pacific"),
))


def corrected_edges(edges, *, regional_review=False):
    removed = REMOVED | edge_pairs((("kazakhstan", "russia"),)) if regional_review else REMOVED
    return sorted((edge_pairs(edges) - removed) | ADDED)


def corrected_straits(straits):
    return [
        {**strait, "seas": ["north-sea", "mediterranean"]}
        if strait["land"] == "north-africa" and set(strait["seas"]) == {"north-atlantic", "mediterranean"}
        else strait
        for strait in straits
    ]
