"""Cart operations: add/remove items, pickup time, validation, summaries."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from dddcli.menu import Item
from dddcli.toast import ToastError, Transport

TOAST_TIME = "%Y-%m-%dT%H:%M:%S+0000"


def parse_toast_time(text: str) -> datetime:
    """Toast writes '2026-09-06T19:15:00+0000' in one place and '2026-09-06T19:30:00.000+00:00' in another."""
    t = text.strip()
    if t.endswith("+0000"):
        t = t[:-5] + "+00:00"
    elif t.endswith("Z"):
        t = t[:-1] + "+00:00"
    return datetime.fromisoformat(t).astimezone(timezone.utc)


class CartError(ToastError):
    def __init__(self, code: str | None, message: str, items: list | None = None):
        self.code = code
        self.items = items or []
        detail = message
        if self.items:
            detail += " (" + ", ".join(i.get("name", "?") for i in self.items) + ")"
        super().__init__(detail)


def create_cart_input(restaurant_guid: str) -> dict:
    return {
        "restaurantGuid": restaurant_guid,
        "orderSource": "ONLINE",
        "cartFulfillmentInput": {"fulfillmentType": "ASAP"},
        "digitalSurface": "OO_BASIC",
        "channelGuid": None,
    }


def _with_quantity(groups: list[dict], quantity: int) -> list[dict]:
    """Toast counts modifiers per parent quantity: two dogs with Grilled need Grilled x2,
    or the server answers "select all required modifiers"."""
    out = []
    for g in groups:
        mods = [{**m, "quantity": quantity, "modifierGroups": _with_quantity(m.get("modifierGroups") or [], quantity)}
                for m in g.get("modifiers") or []]
        out.append({**g, "modifiers": mods})
    return out


def selection_input(item: Item, quantity: int, modifiers: list[dict], note: str | None) -> dict:
    return {
        "itemGuid": item.guid,
        "itemGroupGuid": item.group_guid,
        "itemMasterId": item.master_id,
        "quantity": quantity,
        "specialInstructions": note or "",
        "fractionalQuantity": None,
        "modifierGroups": _with_quantity(modifiers, quantity),
    }


def _unwrap(resp: dict | None, op: str) -> dict:
    if not resp:
        raise ToastError(f"{op}: empty response")
    kind = resp.get("__typename", "")
    if kind == "CartResponse":
        return resp
    if kind in ("CartModificationError", "CartError", "CartValidationError", "ApplyPromoCodeError"):
        raise CartError(resp.get("code") or resp.get("key"), resp.get("message") or kind)
    if kind == "CartOutOfStockError":
        raise CartError("OUT_OF_STOCK", resp.get("message") or "out of stock", resp.get("items"))
    raise ToastError(f"{op}: unexpected {kind}: {resp}")


def add(t: Transport, cart_guid: str | None, selection: dict) -> dict:
    data = t.mutate(
        "AddToCart",
        {
            "input": {
                "cartGuid": cart_guid,
                "createCartInput": None if cart_guid else create_cart_input(t.restaurant_guid()),
                "selection": selection,
            }
        },
    )
    return _unwrap(data.get("addItemToCartV2"), "AddToCart")["cart"]


def get(t: Transport, cart_guid: str) -> dict | None:
    data = t.query("Cart", {"guid": cart_guid, "totalGiftCardBalance": 0})
    resp = data.get("cartV2") or {}
    if resp.get("__typename") == "CartError":
        if resp.get("code") == "CART_NOT_FOUND":
            return None
        raise CartError(resp.get("code"), resp.get("message") or "cart error")
    cart = resp.get("cart")
    if cart is not None:
        cart["guid"] = cart_guid  # the Cart query does not echo the guid; callers rely on it
    return cart


def delete(t: Transport, cart_guid: str, selection_guid: str) -> None:
    data = t.mutate("DeleteFromCart", {"input": {"cartGuid": cart_guid, "selectionGuid": selection_guid}})
    _unwrap(data.get("deleteItemFromCartV2"), "DeleteFromCart")


def set_pickup(t: Transport, cart_guid: str, when: datetime | None) -> dict:
    fulfillment = {"fulfillmentType": "ASAP", "diningOptionBehavior": "TAKE_OUT"}
    if when is not None:
        fulfillment = {
            "fulfillmentType": "FUTURE",
            "diningOptionBehavior": "TAKE_OUT",
            "fulfillmentDateTime": when.astimezone(timezone.utc).strftime(TOAST_TIME),
        }
    data = t.mutate(
        "UpdateFulfillment",
        {"input": {"cartGuid": cart_guid, "cartFulfillmentInput": fulfillment, "skipPrecheckoutValidation": True}},
    )
    return _unwrap(data.get("updateFulfillmentAndValidate"), "UpdateFulfillment")["cart"]


def availability(t: Transport, days_ahead: int = 3) -> dict:
    data = t.query("OOAvailability", {"restaurantGuid": t.restaurant_guid(), "fulfillmentDaysAhead": days_ahead})
    enabled = (data.get("restaurantV2") or {}).get("onlineOrderingEnabled")
    takeout = next((o for o in data.get("diningOptions") or [] if o.get("behavior") == "TAKE_OUT"), None)
    slots: list[datetime] = []
    asap = False
    if takeout:
        asap = bool((takeout.get("asapSchedule") or {}).get("availableNow"))
        for d in (takeout.get("futureSchedule") or {}).get("dates") or []:
            for s in d.get("times") or []:
                slots.append(parse_toast_time(s["time"]))
    return {"enabled": enabled, "takeout": bool(takeout), "asap": asap, "slots": slots}


def schedule(t: Transport) -> dict:
    data = t.query("RestaurantSchedules", {"restaurantGuids": [t.restaurant_guid()]})
    rows = data.get("restaurants") or []
    if not rows:
        raise ToastError("No schedule returned for this restaurant.")
    return rows[0]


def validate(t: Transport, cart_guid: str, customer: dict) -> dict:
    data = t.mutate(
        "ValidateCartPreCheckout",
        {"cartGuid": cart_guid, "validateOrderTime": True, "customer": customer, "sessionId": None},
    )
    return _unwrap(data.get("validateCartPreCheckout"), "ValidateCartPreCheckout")


# ---- time helpers -------------------------------------------------------------

def parse_when(text: str, now: datetime, tz: ZoneInfo) -> datetime | None:
    """'asap' -> None; otherwise an aware datetime in the restaurant's zone.

    Accepts HH:MM, H:MMam/pm, 'tomorrow HH:MM', 'YYYY-MM-DD HH:MM', '+30m', '+1h'.
    A bare clock time means today, or tomorrow if it has already passed.
    """
    s = text.strip().lower()
    if s in ("asap", "now", ""):
        return None
    local_now = now.astimezone(tz)
    if s.startswith("+"):
        num = s[1:].rstrip("mh")
        if not num.isdigit():
            raise ToastError(f"Cannot read {text!r}; try +30m or +1h.")
        delta = timedelta(hours=int(num)) if s.endswith("h") else timedelta(minutes=int(num))
        return local_now + delta
    day = local_now.date()
    if s.startswith("tomorrow"):
        day = day + timedelta(days=1)
        s = s[len("tomorrow"):].strip()
    elif s.startswith("today"):
        s = s[len("today"):].strip()
    elif len(s) >= 10 and s[4] == "-" and s[7] == "-":
        day = datetime.strptime(s[:10], "%Y-%m-%d").date()
        s = s[10:].strip()
    clock = None
    for fmt in ("%H:%M", "%I:%M%p", "%I%p", "%I:%M %p", "%I %p"):
        try:
            clock = datetime.strptime(s, fmt).time()
            break
        except ValueError:
            continue
    if clock is None:
        raise ToastError(f"Cannot read a pickup time from {text!r}. Try 12:30, 6pm, 'tomorrow 12:15', or +30m.")
    when = datetime.combine(day, clock, tzinfo=tz)
    if when < local_now and not text.strip().lower().startswith(("tomorrow", "today", "2")):
        when += timedelta(days=1)
    return when


def snap_to_slot(when: datetime, slots: list[datetime], tolerance: timedelta = timedelta(minutes=20)) -> datetime:
    """The first offered slot at or after `when` within `tolerance`, else a helpful error."""
    later = [s for s in slots if s >= when - timedelta(seconds=30)]
    if later and later[0] - when <= tolerance:
        return later[0]
    if not slots:
        raise ToastError("The restaurant is not offering any pickup times right now.")
    near = sorted(slots, key=lambda s: abs(s - when))[:4]
    tz = when.tzinfo
    raise ToastError(
        "No pickup slot near " + when.strftime("%a %-I:%M %p") + ". Nearest offered: "
        + ", ".join(s.astimezone(tz).strftime("%a %-I:%M %p") for s in sorted(near))
    )


def money(x) -> str:
    return f"${float(x or 0):,.2f}"


def summary(cart: dict, tz: ZoneInfo) -> str:
    order = cart.get("order") or {}
    lines = []
    for i, s in enumerate(order.get("selections") or [], 1):
        mods = ", ".join(m["name"] for m in s.get("modifiers") or [] if m.get("name"))
        qty = s.get("quantity") or 1
        lines.append(f"{i:>2}. {qty} x {s['name']:<28} {money(s.get('price')):>8}" + (f"\n      {mods}" if mods else ""))
    if not lines:
        lines.append("   (empty)")
    subtotal = order.get("preDiscountItemsSubtotal")
    if subtotal is None:
        subtotal = float(order.get("totalV2") or 0) - float(order.get("taxV2") or 0)
    lines.append(f"    {'Subtotal':<32} {money(subtotal):>8}")
    if order.get("discountsTotal"):
        lines.append(f"    {'Discounts':<32} -{money(order.get('discountsTotal')):>7}")
    lines.append(f"    {'Tax':<32} {money(order.get('taxV2')):>8}")
    lines.append(f"    {'Total':<32} {money(order.get('totalV2')):>8}")
    when = cart.get("fulfillmentDateTime")
    if cart.get("fulfillmentType") == "FUTURE" and when:
        local = parse_toast_time(when).astimezone(tz)
        lines.append(f"    Pickup: {local.strftime('%A %-I:%M %p')}")
    else:
        lines.append(f"    Pickup: ASAP (about {cart.get('takeoutQuoteTime') or cart.get('quoteTime') or '?'} min)")
    return "\n".join(lines)
