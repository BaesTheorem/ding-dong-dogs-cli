"""ddd: order takeout from Ding Dong Dogs from the terminal."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from dddcli import __version__, cart as cartmod, checkout, menu as menumod
from dddcli.store import Store
from dddcli.toast import DEFAULT_SLUG, ToastError, Transport

DEFAULT_TZ = "America/Chicago"


class Cli:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.store = Store()
        self.t = Transport(self.store, args.restaurant, verbose=args.verbose)
        self.state = self.t.state
        self._items: list[menumod.Item] | None = None

    # ---- helpers -------------------------------------------------------------------

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.state.get("tz") or DEFAULT_TZ)

    def out(self, text: str = "", data=None) -> None:
        if self.args.json:
            print(json.dumps(data if data is not None else text, indent=1, default=str))
        else:
            print(text)

    def items(self) -> list[menumod.Item]:
        if self._items is None:
            self._items = menumod.flatten(menumod.fetch_menus(self.t))
        return self._items

    def cart_guid(self) -> str | None:
        c = self.state.get("cart") or {}
        if c.get("guid") and (not c.get("expires") or c["expires"] / 1000 > time.time()):
            return c["guid"]
        return None

    def remember_cart(self, cart: dict | None) -> None:
        if cart:
            self.state["cart"] = {"guid": cart["guid"], "expires": cart.get("expiredDate")}
        else:
            self.state.pop("cart", None)
        self.store.save_state()

    def current_cart(self) -> dict | None:
        guid = self.cart_guid()
        if not guid:
            return None
        cart = cartmod.get(self.t, guid)
        if cart is None:
            self.remember_cart(None)
        return cart

    def ensure_tz(self) -> None:
        if not self.state.get("tz"):
            sched = cartmod.schedule(self.t)
            self.state["tz"] = sched.get("timeZoneId") or DEFAULT_TZ
            self.store.save_state()

    # ---- commands ------------------------------------------------------------------

    def cmd_menu(self) -> int:
        menus = menumod.fetch_menus(self.t)
        want = (self.args.group or "").lower()
        if self.args.json:
            self.out(data=[it.as_dict() for it in menumod.flatten(menus) if not want or want in it.group.lower()])
            return 0
        for m in menus:
            for g in m.get("groups") or []:
                if want and want not in g["name"].lower():
                    continue
                print(f"\n{g['name']}  ({m['name']})")
                for it in g.get("items") or []:
                    oos = it.get("outOfStock")
                    if oos and not self.args.all:
                        continue
                    price = (it.get("prices") or [0])[0]
                    mark = "  (out of stock)" if oos else ("  *" if it.get("hasModifiers") else "")
                    desc = f"  {it['description']}" if it.get("description") else ""
                    print(f"  {it['name']:<28} {cartmod.money(price):>8}{mark}{desc}")
        print("\n* has choices; `ddd item NAME` lists them")
        return 0

    def cmd_search(self) -> int:
        q = " ".join(self.args.text).lower()
        hits = [it for it in self.items() if q in it.name.lower() or q in it.description.lower() or q in it.group.lower()]
        if self.args.json:
            self.out(data=[it.as_dict() for it in hits])
            return 0
        if not hits:
            print(f"Nothing on the menu matches {q!r}. Toppings and upgrades are choices inside an item: `ddd item hot dog`.")
            return 1
        for it in hits:
            flag = "  (out of stock)" if it.out_of_stock else ""
            print(f"{it.name:<28} {cartmod.money(it.price):>8}  {it.group}{flag}")
        return 0

    def cmd_item(self) -> int:
        it = menumod.find(self.items(), " ".join(self.args.name))
        d = menumod.details(self.t, it)
        if self.args.json:
            self.out(data=d)
        else:
            print(menumod.describe(d))
        return 0

    def cmd_hours(self) -> int:
        sched = cartmod.schedule(self.t)
        self.state["tz"] = sched.get("timeZoneId") or DEFAULT_TZ
        self.store.save_state()
        avail = cartmod.availability(self.t, days_ahead=3)
        if self.args.json:
            self.out(data={"schedule": sched, "asap": avail["asap"], "slots": avail["slots"]})
            return 0
        s = sched.get("schedule") or {}
        today = s.get("todaysHoursForTakeout")
        print("Online ordering: " + ("on" if sched.get("onlineOrderingEnabled") else "OFF"))
        print("Takeout today: " + (f"{_clock(today['startTime'])} to {_clock(today['endTime'])}" if today else "closed"))
        print("ASAP pickup: " + ("available now" if avail["asap"] else "not right now"))
        for up in s.get("upcomingSchedules") or []:
            if up.get("behavior") != "TAKE_OUT":
                continue
            print("\nUpcoming takeout hours:")
            for day in up.get("dailySchedules") or []:
                periods = ", ".join(f"{_clock(p['startTime'])} to {_clock(p['endTime'])}" for p in day.get("servicePeriods") or [])
                label = datetime.strptime(day["date"], "%Y-%m-%d").strftime("%a %b %-d")
                note = f"  ({day['overrideDescription']})" if day.get("overrideDescription") else ""
                print(f"  {label:<11} {periods or 'closed'}{note}")
        if avail["slots"]:
            nxt = [x.astimezone(self.tz).strftime("%a %-I:%M %p") for x in avail["slots"][:4]]
            print("\nNext pickup slots: " + ", ".join(nxt) + f"  (+{max(0, len(avail['slots']) - 4)} more)")
        return 0

    def cmd_add(self) -> int:
        it = menumod.find(self.items(), " ".join(self.args.name))
        if it.out_of_stock:
            raise ToastError(f"{it.name} is out of stock right now.")
        mods: list[dict] = []
        labels: list[str] = []
        if it.has_modifiers or self.args.mod:
            d = menumod.details(self.t, it)
            mods, labels = menumod.resolve_modifiers(d, self.args.mod or [])
        selection = cartmod.selection_input(it, self.args.qty, mods, self.args.note)
        cart = cartmod.add(self.t, self.cart_guid(), selection)
        self.remember_cart(cart)
        self.ensure_tz()
        cart = cartmod.get(self.t, cart["guid"]) or cart
        if self.args.json:
            self.out(data=cart)
            return 0
        print(f"Added {self.args.qty} x {it.name}" + (" with " + "; ".join(labels) if labels else ""))
        print(cartmod.summary(cart, self.tz))
        return 0

    def cmd_cart(self) -> int:
        cart = self.current_cart()
        if self.args.json:
            self.out(data=cart)
            return 0
        if not cart:
            print("Cart is empty. `ddd menu` to browse, `ddd add NAME` to start one.")
            return 0
        self.ensure_tz()
        print(cartmod.summary(cart, self.tz))
        return 0

    def cmd_remove(self) -> int:
        cart = self.current_cart()
        if not cart:
            raise ToastError("Cart is empty.")
        sels = (cart.get("order") or {}).get("selections") or []
        targets = []
        for ref in self.args.which:
            if ref.isdigit() and 1 <= int(ref) <= len(sels):
                targets.append(sels[int(ref) - 1])
                continue
            hits = [s for s in sels if s["name"].lower() == ref.lower()] or [s for s in sels if ref.lower() in s["name"].lower()]
            if not hits:
                raise ToastError(f"Nothing in the cart matches {ref!r}.")
            targets.append(hits[0])
        for s in targets:
            cartmod.delete(self.t, cart["guid"], s["guid"])
            print(f"Removed {s['name']}")
        cart = self.current_cart()
        if cart:
            print(cartmod.summary(cart, self.tz))
        return 0

    def cmd_clear(self) -> int:
        cart = self.current_cart()
        if cart:
            for s in (cart.get("order") or {}).get("selections") or []:
                cartmod.delete(self.t, cart["guid"], s["guid"])
        self.remember_cart(None)
        print("Cart cleared.")
        return 0

    def cmd_pickup(self) -> int:
        self.ensure_tz()
        avail = cartmod.availability(self.t, days_ahead=3)
        if not self.args.when:
            cart = self.current_cart()
            if self.args.json:
                self.out(data={"asap": avail["asap"], "slots": avail["slots"], "cart": cart})
                return 0
            print("ASAP pickup: " + ("available now" if avail["asap"] else "not right now"))
            if avail["slots"]:
                by_day: dict[str, list[str]] = {}
                for s in avail["slots"]:
                    local = s.astimezone(self.tz)
                    by_day.setdefault(local.strftime("%A %b %-d"), []).append(local.strftime("%-I:%M %p"))
                for day, times in by_day.items():
                    print(f"  {day}: {times[0]} to {times[-1]} ({len(times)} slots)")
            if cart:
                print(cartmod.summary(cart, self.tz).splitlines()[-1].strip())
            return 0
        guid = self.cart_guid()
        if not guid:
            raise ToastError("Add something to the cart first; the pickup time is stored on the cart.")
        when = cartmod.parse_when(" ".join(self.args.when), datetime.now(timezone.utc), self.tz)
        if when is None:
            if not avail["asap"]:
                raise ToastError("ASAP pickup is not available right now; pick a time from `ddd pickup`.")
            cart = cartmod.set_pickup(self.t, guid, None)
        else:
            slot = cartmod.snap_to_slot(when, avail["slots"])
            cart = cartmod.set_pickup(self.t, guid, slot)
        self.remember_cart(cart)
        cart = self.current_cart() or cart
        if self.args.json:
            self.out(data=cart)
        else:
            print(cartmod.summary(cart, self.tz))
        return 0

    def cmd_profile(self) -> int:
        p = self.store.profile
        if self.args.first or self.args.last or self.args.email or self.args.phone or self.args.tip_pct is not None:
            for k in ("first", "last", "email", "phone"):
                v = getattr(self.args, k)
                if v:
                    p[k] = v.strip()
            if self.args.tip_pct is not None:
                p["tip_pct"] = self.args.tip_pct
            self.store.save_profile(p)
        if self.args.json:
            self.out(data=p)
            return 0
        if not p:
            print("No profile yet. `ddd profile --first Alex --last H --email you@example.com --phone 8165551234`")
            return 1
        for k in ("first", "last", "email", "phone", "tip_pct"):
            if k in p:
                print(f"{k:<8} {p[k]}")
        print(f"(stored in {self.store.root})")
        return 0

    def cmd_card(self) -> int:
        action = self.args.action
        if action == "set":
            card = checkout.prompt_card(" ".join(x for x in (self.store.profile.get("first"), self.store.profile.get("last")) if x) or None)
            checkout.save_card(card)
            print(f"Saved {card.label} to the macOS Keychain.")
            return 0
        if action == "clear":
            print("Removed the saved card." if checkout.clear_card() else "No saved card.")
            return 0
        card = checkout.load_card()
        print(card.label if card else "No saved card. `ddd card set` stores one in the Keychain; otherwise checkout asks.")
        return 0

    def customer(self) -> dict:
        p = self.store.profile
        missing = [k for k in ("first", "last", "email", "phone") if not p.get(k)]
        if missing:
            raise ToastError("Profile is missing " + ", ".join(missing) + ". Set it with `ddd profile --first ... --last ... --email ... --phone ...`.")
        return {"firstName": p["first"], "lastName": p["last"], "email": p["email"], "phone": "".join(ch for ch in p["phone"] if ch.isdigit())}

    def cmd_checkout(self) -> int:
        cart = self.current_cart()
        if not cart or not (cart.get("order") or {}).get("selections"):
            raise ToastError("Cart is empty.")
        self.ensure_tz()
        customer = self.customer()
        order = cart["order"]
        subtotal = float(order.get("preDiscountItemsSubtotal") or 0)
        tip = 0.0
        if self.args.tip is not None:
            tip = float(self.args.tip)
        else:
            pct = self.args.tip_pct if self.args.tip_pct is not None else self.store.profile.get("tip_pct")
            if pct:
                tip = round(subtotal * float(pct) / 100, 2)
        total = float(order.get("totalV2") or 0) + tip
        print(cartmod.summary(cart, self.tz))
        print(f"    {'Tip':<32} {cartmod.money(tip):>8}")
        print(f"    {'Charge':<32} {cartmod.money(total):>8}")
        print(f"    For: {customer['firstName']} {customer['lastName']}, {customer['email']}, {customer['phone']}")
        result = cartmod.validate(self.t, cart["guid"], customer)
        for w in (result.get("warnings") or []) + (result.get("info") or []):
            print(f"    Note from Toast: {w.get('message')}")
        if self.args.dry_run:
            print("Dry run: cart validated, nothing charged.")
            return 0
        card = checkout.load_card()
        if card and not self.args.new_card:
            print(f"    Paying with saved {card.label}")
        if not self.args.yes:
            answer = input(f"Place this order for {cartmod.money(total)}? [y/N] ").strip().lower()
            if answer not in ("y", "yes"):
                print("Not ordered.")
                return 1
        if not card or self.args.new_card:
            card = checkout.prompt_card(f"{customer['firstName']} {customer['lastName']}")
        intent = checkout.create_intent(self.t, cart["guid"])
        checkout.update_intent(self.t, cart["guid"], intent, customer["email"], tip, float(order.get("taxV2") or 0))
        token = checkout.client_token(self.t)
        pm = checkout.create_payment_method(self.t, token, intent, card, customer["email"])
        pm_id = pm.get("id") or pm.get("paymentMethodId")
        if not pm_id:
            raise checkout.PaymentError(f"Card was not accepted: {pm}")
        confirmed = checkout.confirm_payment(self.t, token, intent, pm_id, customer["email"])
        self.t._log("confirm result", json.dumps(confirmed)[:400])  # noqa: SLF001
        payment = confirmed.get("payment") or confirmed
        ref = payment.get("externalReferenceId") or intent["id"]
        who = {**customer, "phoneCountryCode": "1"}
        try:
            done = checkout.place_order(self.t, cart["guid"], who, tip, intent, pm_id, intent_ref=ref)
        except checkout.PaymentError as first:
            if ref == intent["id"]:
                raise
            self.t._log("retrying placeSpiOrder with the intent id after:", str(first))  # noqa: SLF001
            done = checkout.place_order(self.t, cart["guid"], who, tip, intent, pm_id, intent_ref=intent["id"])
        self.state["last_order"] = {"guid": done.get("guid"), "placed": time.time()}
        self.remember_cart(None)
        if self.args.json:
            self.out(data=done)
            return 0
        print(_order_text(done, self.tz))
        return 0

    def cmd_order(self) -> int:
        guid = self.args.guid or (self.state.get("last_order") or {}).get("guid")
        if not guid:
            raise ToastError("No order to show; pass an order guid.")
        done = checkout.completed_order(self.t, guid)
        if self.args.json:
            self.out(data=done)
        else:
            print(_order_text(done, self.tz) if done else "Toast returned nothing for that order.")
        return 0

    def cmd_refresh(self) -> int:
        mapping = self.t.refresh_hashes()
        if self.args.print:
            self.out(data=mapping) if self.args.json else print(json.dumps(mapping, indent=1, sort_keys=True))
        else:
            print(f"Refreshed {len(mapping)} operation hashes (client version {self.t.client_version}).")
        return 0

    def cmd_raw(self) -> int:
        variables = json.loads(self.args.variables) if self.args.variables else {}
        data = self.t.mutate(self.args.operation, variables) if self.args.mutation else self.t.query(self.args.operation, variables)
        print(json.dumps(data, indent=1))
        return 0


def _clock(t: str) -> str:
    return datetime.strptime(t[:5], "%H:%M").strftime("%-I:%M %p")


def _order_text(o: dict, tz: ZoneInfo) -> str:
    lines = [f"Order placed: check #{o.get('checkNumber')}  ({o.get('approvalStatus')})"]
    when = o.get("estimatedFulfillmentDate") or o.get("promisedDateTime")
    if when:
        try:
            dt = datetime.fromisoformat(str(when).replace("Z", "+00:00")).astimezone(tz)
            lines.append(f"Ready around {dt.strftime('%A %-I:%M %p')}")
        except ValueError:
            lines.append(f"Ready: {when}")
    for s in o.get("selections") or []:
        mods = ", ".join(m.get("name", "") for m in s.get("modifiers") or [])
        lines.append(f"  {s.get('quantity')} x {s.get('name')}" + (f" ({mods})" if mods else ""))
    lines.append(f"  Subtotal {cartmod.money(o.get('itemsSubtotal'))}, tax {cartmod.money(o.get('taxV2'))}, tip {cartmod.money(o.get('tip'))}, total {cartmod.money(o.get('totalV2'))}")
    for p in o.get("payments") or []:
        lines.append(f"  Paid {cartmod.money(p.get('totalAmount'))} with {p.get('cardType') or p.get('type')} ending {p.get('last4Digits')}: {p.get('status')}")
    loc = (o.get("restaurant") or {}).get("location") or {}
    if loc.get("address1"):
        lines.append(f"  Pick up at {loc['address1']}, {loc.get('city')}")
    if o.get("guid"):
        lines.append(f"  Order id {o['guid']}")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ddd", description="Order takeout from Ding Dong Dogs (Kansas City) from the terminal.")
    p.add_argument("-r", "--restaurant", default=os.environ.get("DDD_RESTAURANT", DEFAULT_SLUG), help="Toast restaurant slug (the part after /online/ in the ordering URL)")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument("-v", "--verbose", action="store_true", help="log every request")
    p.add_argument("--version", action="version", version=f"ddd {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("menu", help="the whole menu"); s.add_argument("--all", action="store_true", help="include out-of-stock items"); s.add_argument("-g", "--group", help="only this menu group")
    s = sub.add_parser("search", help="find menu items by word"); s.add_argument("text", nargs="+")
    s = sub.add_parser("item", help="one item with its choices"); s.add_argument("name", nargs="+")
    sub.add_parser("hours", help="hours, ASAP availability, next pickup slots")
    s = sub.add_parser("add", help="add an item to the cart")
    s.add_argument("name", nargs="+"); s.add_argument("-n", "--qty", type=int, default=1)
    s.add_argument("-m", "--mod", action="append", help='choice, e.g. -m "Cook Type=Grilled" -m "Toppings=Chili,Diced Onion" or just -m Grilled')
    s.add_argument("--note", help="special request for the kitchen")
    sub.add_parser("cart", help="show the cart")
    s = sub.add_parser("remove", help="remove cart lines by number or name"); s.add_argument("which", nargs="+")
    sub.add_parser("clear", help="empty the cart")
    s = sub.add_parser("pickup", help="show or set the pickup time"); s.add_argument("when", nargs="*", help="asap, 12:30, 6pm, 'tomorrow 12:15', +30m")
    s = sub.add_parser("checkout", help="pay and place the order")
    s.add_argument("--tip", type=float, help="tip in dollars"); s.add_argument("--tip-pct", type=float, help="tip as a percent of the subtotal")
    s.add_argument("-y", "--yes", action="store_true", help="skip the confirmation prompt"); s.add_argument("--dry-run", action="store_true", help="validate the cart, charge nothing")
    s.add_argument("--new-card", action="store_true", help="ignore the saved card and type one")
    s = sub.add_parser("order", help="show the last (or a given) order"); s.add_argument("guid", nargs="?")
    s = sub.add_parser("profile", help="who the order is for")
    s.add_argument("--first"); s.add_argument("--last"); s.add_argument("--email"); s.add_argument("--phone"); s.add_argument("--tip-pct", type=float, help="default tip percent for checkout")
    s = sub.add_parser("card", help="saved card in the macOS Keychain"); s.add_argument("action", nargs="?", choices=["show", "set", "clear"], default="show")
    s = sub.add_parser("refresh", help="re-read Toast's operation hashes from the live web bundle"); s.add_argument("--print", action="store_true")
    s = sub.add_parser("raw", help="run a persisted operation by name"); s.add_argument("operation"); s.add_argument("variables", nargs="?"); s.add_argument("--mutation", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cli = Cli(args)
    handler = getattr(cli, f"cmd_{args.cmd}")
    try:
        return handler()
    except menumod.Ambiguous as e:
        print(f"{e}", file=sys.stderr)
        return 2
    except ToastError as e:
        print(f"ddd: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print()
        return 130
