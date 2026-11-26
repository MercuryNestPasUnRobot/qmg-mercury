"""Apply a private map variant over a fixed card catalog for a new match."""

from copy import deepcopy

from .definitions import CONNECTION_USES, PER_UNIT_SITE, RuleSet
from .model import CoreError


CURRENT_BOARD_RULES = {"round_end_victory": True, "occupied_home_blocks_scoring": True, "discard_phase_penalty": 1}


def map_layout(rules):
    edges = {}
    for row in rules.connections.values():
        pair = tuple(sorted((row["a"], row["b"])))
        edges.setdefault(pair, set()).update(row.get("movement_or_supply_tags", CONNECTION_USES))
    return {"connections": [{"a": a, "b": b, "uses": sorted(uses)} for (a, b), uses in sorted(edges.items())],
            "straits": rules.settings.get("straits", []),
            "points": {row["region"]: row["vp"] for row in rules.sites.values() if not row.get("dynamic")}}


def match_rules(base, layout=None):
    data = deepcopy(base.data)
    data["settings"].update(CURRENT_BOARD_RULES)
    if layout is None:
        return RuleSet(data)
    if not isinstance(layout, dict) or set(layout) != {"connections", "straits", "points"}:
        raise CoreError("地图配置不完整。")
    links, straits, points = layout["connections"], layout["straits"], layout["points"]
    if not isinstance(links, list) or not isinstance(straits, list) or not isinstance(points, dict):
        raise CoreError("地图配置的格式有误。")

    regions = base.regions
    pairs = set()
    connections = []
    for link in links:
        if not isinstance(link, dict) or set(link) != {"a", "b", "uses"}:
            raise CoreError("连接必须有两个地区及用途。")
        a, b, uses = link["a"], link["b"], link["uses"]
        if (not isinstance(a, str) or not isinstance(b, str) or a not in regions or b not in regions or a == b or
                not isinstance(uses, list) or not uses or any(not isinstance(use, str) or use not in CONNECTION_USES for use in uses)):
            raise CoreError("连接的地区或用途无效。")
        pair = tuple(sorted((a, b)))
        if pair in pairs:
            raise CoreError("存在重复的地区连接。")
        pairs.add(pair)
        connections.append({"id": f"map-edge-{len(connections)}", "a": pair[0], "b": pair[1],
                            "movement_or_supply_tags": sorted(set(uses))})
    channels = set()
    for strait in straits:
        if not isinstance(strait, dict) or set(strait) != {"land", "seas"}:
            raise CoreError("海峡必须有一个控制陆地和两个海域。")
        land, seas = strait["land"], strait["seas"]
        if (not isinstance(land, str) or regions.get(land, {}).get("type") != "LAND" or not isinstance(seas, list) or
                len(seas) != 2 or any(not isinstance(sea, str) or regions.get(sea, {}).get("type") != "SEA" for sea in seas)
                or seas[0] == seas[1]):
            raise CoreError("请为海峡选择控制陆地和两个不同的海域。")
        pair = tuple(sorted(seas))
        if pair in pairs or pair in channels:
            raise CoreError("同一对海域只能有一条普通连接或海峡。")
        channels.add(pair)

    for region, vp in points.items():
        if region not in regions or type(vp) is not int or vp not in (0, 1, 2):
            raise CoreError("分点只能设置为无、半分点或全分点。")
    sites = []
    handled = set()
    for site in data["score_sites"]:
        region = site["region"]
        if site.get("dynamic") or region not in points:
            sites.append(site)
            continue
        if region not in handled and (vp := points[region]):
            site.update(vp=vp, scoring=PER_UNIT_SITE if vp == 1 else "regular")
            sites.append(site)
        handled.add(region)
    for region in sorted(points.keys() - handled):
        if vp := points[region]:
            sites.append({"id": "map-score-" + region, "region": region, "vp": vp, "countries": list(base.countries),
                          "requires_supply": True, "scoring": PER_UNIT_SITE if vp == 1 else "regular"})
    data["connections"], data["score_sites"], data["settings"]["straits"] = connections, sites, straits
    return RuleSet(data)
