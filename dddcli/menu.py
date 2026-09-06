"""Menu reading and item/modifier resolution."""

from __future__ import annotations

from dataclasses import asdict, dataclass

from dddcli.toast import ToastError, Transport


class NotFound(ToastError):
    pass


class Ambiguous(ToastError):
    def __init__(self, ref: str, candidates: list[str]):
        self.candidates = candidates
        super().__init__(f"{ref!r} matches several things: " + ", ".join(candidates))


@dataclass
class Item:
    name: str
    guid: str
    group_guid: str
    group: str
    menu: str
    price: float
    out_of_stock: bool
    has_modifiers: bool
    master_id: str | None
    description: str

    def as_dict(self) -> dict:
        return asdict(self)


def menu_input(restaurant_guid: str) -> dict:
    return {
        "restaurantGuid": restaurant_guid,
        "respectAvailability": True,
        "hideOutOfStockItems": False,
        "filters": None,
        "offset": 0,
        "slug": None,
        "consolidateItemsByProduct": False,
        "includeSlugs": False,
    }


def fetch_menus(t: Transport) -> list[dict]:
    data = t.query("PaginatedMenuItems", {"input": menu_input(t.restaurant_guid())})
    return (data.get("paginatedMenuItems") or {}).get("menus") or []


def flatten(menus: list[dict]) -> list[Item]:
    items: list[Item] = []
    for menu in menus:
        for group in menu.get("groups") or []:
            for it in group.get("items") or []:
                prices = it.get("prices") or [0]
                items.append(
                    Item(
                        name=it["name"],
                        guid=it["guid"],
                        group_guid=it.get("itemGroupGuid") or group["guid"],
                        group=group["name"],
                        menu=menu["name"],
                        price=float(prices[0] if prices else 0),
                        out_of_stock=bool(it.get("outOfStock")),
                        has_modifiers=bool(it.get("hasModifiers")),
                        master_id=it.get("masterId"),
                        description=it.get("description") or "",
                    )
                )
    return items


def _match(names: list[str], ref: str) -> list[int]:
    """Indexes matching ref: exact (case-insensitive) beats prefix beats substring."""
    r = ref.strip().lower()
    for test in (lambda n: n == r, lambda n: n.startswith(r), lambda n: r in n):
        hits = [i for i, n in enumerate(names) if test(n.lower())]
        if hits:
            return hits
    return []


def find(items: list[Item], ref: str) -> Item:
    for it in items:
        if it.guid == ref:
            return it
    hits = _match([it.name for it in items], ref)
    if not hits:
        raise NotFound(f"No menu item matches {ref!r}. Try `ddd search {ref}`.")
    if len(hits) > 1:
        # Same name in two groups (Kids Menu "Hot Dog"): prefer the one that is not a kids/child menu.
        chosen = [items[i] for i in hits]
        adult = [it for it in chosen if "kid" not in it.group.lower() and "kid" not in it.menu.lower()]
        if len(adult) == 1:
            return adult[0]
        raise Ambiguous(ref, [f"{it.name} ({it.group})" for it in chosen])
    return items[hits[0]]


def details(t: Transport, item: Item) -> dict:
    data = t.query(
        "MenuItemDetails",
        {
            "input": {
                "itemGuid": item.guid,
                "itemGroupGuid": item.group_guid,
                "restaurantGuid": t.restaurant_guid(),
                "dateTime": None,
                "channelGuid": None,
                "includeSlugs": False,
            },
            "nestingLevel": 10,
        },
    )
    d = data.get("menuItemDetails")
    if not d:
        raise NotFound(f"Toast has no details for {item.name!r} any more; the menu may have changed.")
    return d


def parse_mod_spec(spec: str) -> tuple[str | None, list[str]]:
    """'Group=Choice,Choice' or 'Choice' -> (group or None, [choices])."""
    group, _, rest = spec.partition("=")
    if not rest:
        return None, [c.strip() for c in group.split(",") if c.strip()]
    return group.strip(), [c.strip() for c in rest.split(",") if c.strip()]


def resolve_modifiers(d: dict, specs: list[str]) -> tuple[list[dict], list[str]]:
    """Turn --mod specs into AddToCart modifierGroups, filling in Toast's defaults for
    groups the user did not mention and checking each group's min/max."""
    groups = d.get("modifierGroups") or []
    chosen: dict[str, list[dict]] = {g["guid"]: [] for g in groups}
    labels: list[str] = []

    def pick(group: dict, ref: str) -> dict:
        opts = [m for m in group.get("modifiers") or []]
        hits = _match([m["name"] for m in opts], ref)
        if not hits:
            raise NotFound(f"{group['name']}: no choice matches {ref!r}. Choices: " + ", ".join(m["name"] for m in opts))
        if len(hits) > 1:
            raise Ambiguous(ref, [opts[i]["name"] for i in hits])
        m = opts[hits[0]]
        if m.get("outOfStock"):
            raise NotFound(f"{m['name']} is out of stock right now.")
        return m

    for spec in specs:
        gref, crefs = parse_mod_spec(spec)
        for cref in crefs:
            if gref:
                ghits = _match([g["name"] for g in groups], gref)
                if not ghits:
                    raise NotFound(f"No modifier group matches {gref!r}. Groups: " + ", ".join(g["name"] for g in groups))
                if len(ghits) > 1:
                    raise Ambiguous(gref, [groups[i]["name"] for i in ghits])
                group = groups[ghits[0]]
                m = pick(group, cref)
            else:
                found = []
                for g in groups:
                    hits = _match([m["name"] for m in g.get("modifiers") or []], cref)
                    found += [(g, (g["modifiers"])[i]) for i in hits]
                if not found:
                    raise NotFound(f"No modifier choice matches {cref!r} on {d['name']}. See `ddd item {d['name']}`.")
                exact = [(g, m) for g, m in found if m["name"].lower() == cref.lower()]
                found = exact or found
                if len(found) > 1:
                    raise Ambiguous(cref, [f"{g['name']}={m['name']}" for g, m in found])
                group, m = found[0]
                if m.get("outOfStock"):
                    raise NotFound(f"{m['name']} is out of stock right now.")
            if any(x["itemGuid"] == m["itemGuid"] for x in chosen[group["guid"]]):
                continue
            chosen[group["guid"]].append(m)
            labels.append(f"{group['name']}: {m['name']}" + (f" (+${m['price']:.2f})" if m.get("price") else ""))

    for g in groups:
        if not chosen[g["guid"]]:
            for m in g.get("modifiers") or []:
                if m.get("isDefault") and not m.get("outOfStock"):
                    chosen[g["guid"]].append(m)
                    labels.append(f"{g['name']}: {m['name']} (default)")
        n = len(chosen[g["guid"]])
        lo, hi = g.get("minSelections") or 0, g.get("maxSelections")
        if n < lo:
            raise ToastError(
                f"{d['name']} needs {lo} choice(s) for {g['name']!r}: "
                + ", ".join(m["name"] for m in g.get("modifiers") or [] if not m.get("outOfStock"))
                + f". Use --mod \"{g['name']}=<choice>\"."
            )
        if hi is not None and n > hi:
            raise ToastError(f"{g['name']!r} allows at most {hi} choice(s); you picked {n}.")

    out = []
    for g in groups:
        if chosen[g["guid"]]:
            out.append(
                {
                    "guid": g["guid"],
                    "modifiers": [
                        {"itemGuid": m["itemGuid"], "itemGroupGuid": m.get("itemGroupGuid"), "quantity": 1, "modifierGroups": []}
                        for m in chosen[g["guid"]]
                    ],
                }
            )
    return out, labels


def describe(d: dict) -> str:
    lines = [f"{d['name']}  ${(d.get('prices') or [0])[0]:.2f}" + ("  (out of stock)" if d.get("outOfStock") else "")]
    if d.get("description"):
        lines.append("  " + d["description"])
    for g in d.get("modifierGroups") or []:
        lo, hi = g.get("minSelections") or 0, g.get("maxSelections")
        rule = "required" if lo else "optional"
        if hi == 1:
            rule += ", pick one"
        elif hi:
            rule += f", up to {hi}"
        lines.append(f"  [{g['name']}] ({rule})")
        for m in g.get("modifiers") or []:
            extra = f" +${m['price']:.2f}" if m.get("price") else ""
            flags = (" (default)" if m.get("isDefault") else "") + (" (out of stock)" if m.get("outOfStock") else "")
            lines.append(f"      {m['name']}{extra}{flags}")
    return "\n".join(lines)
