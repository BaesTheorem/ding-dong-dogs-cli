import base64
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from dddcli import cardcrypto, cart, menu, toast
from dddcli.store import Store

TZ = ZoneInfo("America/Chicago")


def test_extract_hashes_pairs_nearest_definition():
    bundle = (
        'Xr=(0,to.Ps)(a||(a=so(["\\n    query Cart($guid: ID!) {\\n  cartV2 {\\n"])));'
        'Yr=(0,to.Ps)(b||(b=so(["\\n    mutation AddToCart($input: X!) {\\n"])));'
        'let q;Xr.__meta__={hash:"aaaa"},Yr.__meta__={hash:"bbbb"};'
        'Xr=(0,to.Ps)(c||(c=so(["\\n    query Other($x: ID!) {\\n"])));Xr.__meta__={hash:"cccc"};'
        '{NODE_ENV:"production",VERSION:"3813"}'
    )
    mapping, version = toast.extract_hashes(bundle)
    assert mapping == {"Cart": "aaaa", "AddToCart": "bbbb", "Other": "cccc"}
    assert version == "3813"


def test_parse_session_decodes_html_entities():
    payload = base64.b64encode(json.dumps({"id": "abc", "issuedAt": "2026-09-06T18:00:00Z", "expiresAt": "2026-09-06T19:00:00Z"}).encode()).decode()
    html = f'<div id="session" data-content="{payload.replace("=", "&#x3D;")}"/>'
    sid, expires = toast.parse_session(html)
    assert sid == "abc"
    assert expires == datetime(2026, 9, 6, 19, 0, tzinfo=timezone.utc).timestamp()


MENUS = [
    {"name": "Main Menu", "groups": [
        {"name": "Dogs", "guid": "g1", "items": [
            {"name": "Hot Dog", "guid": "i1", "itemGroupGuid": "g1", "prices": [7], "outOfStock": False, "hasModifiers": True, "masterId": "m1"},
            {"name": "Chili Dog", "guid": "i2", "itemGroupGuid": "g1", "prices": [8], "outOfStock": False, "hasModifiers": True, "masterId": "m2"},
        ]},
        {"name": "Kids Menu", "guid": "g2", "items": [
            {"name": "Hot Dog", "guid": "i3", "itemGroupGuid": "g2", "prices": [9], "outOfStock": False, "hasModifiers": False, "masterId": "m3"},
        ]},
        {"name": "Sides", "guid": "g3", "items": [
            {"name": "Tater Tots", "guid": "i4", "itemGroupGuid": "g3", "prices": [4], "outOfStock": False, "hasModifiers": False, "masterId": "m4"},
            {"name": "Cheese Tots", "guid": "i5", "itemGroupGuid": "g3", "prices": [5], "outOfStock": True, "hasModifiers": False, "masterId": "m5"},
        ]},
    ]},
]


def test_find_prefers_adult_menu_and_reports_ambiguity():
    items = menu.flatten(MENUS)
    assert menu.find(items, "hot dog").guid == "i1"
    with pytest.raises(menu.Ambiguous):
        menu.find(items, "tots")
    assert menu.find(items, "tater").guid == "i4"
    assert menu.find(items, "i3").group == "Kids Menu"
    with pytest.raises(menu.NotFound):
        menu.find(items, "pizza")


DETAILS = {
    "name": "Hot Dog", "guid": "i1", "itemGroupGuid": "g1", "prices": [7], "masterId": "m1",
    "modifierGroups": [
        {"name": "Cook Type", "guid": "mg1", "minSelections": 1, "maxSelections": 1, "modifiers": [
            {"name": "Grilled", "itemGuid": "c1", "itemGroupGuid": None, "price": 0, "isDefault": False, "outOfStock": False},
            {"name": "Fried", "itemGuid": "c2", "itemGroupGuid": None, "price": 0, "isDefault": False, "outOfStock": False},
        ]},
        {"name": "Toppings", "guid": "mg2", "minSelections": 0, "maxSelections": None, "modifiers": [
            {"name": "Chili", "itemGuid": "t1", "itemGroupGuid": None, "price": 1, "isDefault": False, "outOfStock": False},
            {"name": "Diced Onion", "itemGuid": "t2", "itemGroupGuid": None, "price": 0, "isDefault": False, "outOfStock": False},
            {"name": "Yellow Mustard", "itemGuid": "t3", "itemGroupGuid": None, "price": 0, "isDefault": True, "outOfStock": False},
        ]},
        {"name": "Upgrade to Impossible Dog", "guid": "mg3", "minSelections": 0, "maxSelections": 1, "modifiers": [
            {"name": "Impossible Dog", "itemGuid": "u1", "itemGroupGuid": None, "price": 2, "isDefault": False, "outOfStock": False},
        ]},
    ],
}


def test_resolve_modifiers_by_group_and_bare_choice_with_defaults():
    mods, labels = menu.resolve_modifiers(DETAILS, ["Cook Type=Grilled", "chili,onion", "impossible"])
    by_group = {m["guid"]: [x["itemGuid"] for x in m["modifiers"]] for m in mods}
    assert by_group == {"mg1": ["c1"], "mg2": ["t1", "t2"], "mg3": ["u1"]}
    assert any("Chili" in x and "+$1.00" in x for x in labels)


def test_resolve_modifiers_fills_defaults_and_enforces_min():
    mods, labels = menu.resolve_modifiers(DETAILS, ["fried"])
    by_group = {m["guid"]: [x["itemGuid"] for x in m["modifiers"]] for m in mods}
    assert by_group == {"mg1": ["c2"], "mg2": ["t3"]}
    assert "Toppings: Yellow Mustard (default)" in labels
    with pytest.raises(toast.ToastError, match="needs 1 choice"):
        menu.resolve_modifiers(DETAILS, [])
    with pytest.raises(toast.ToastError, match="at most 1"):
        menu.resolve_modifiers(DETAILS, ["Cook Type=Grilled,Fried"])


def test_selection_and_cart_inputs_match_the_web_app():
    it = menu.find(menu.flatten(MENUS), "tater tots")
    sel = cart.selection_input(it, 2, [], "no salt")
    assert sel == {"itemGuid": "i4", "itemGroupGuid": "g3", "itemMasterId": "m4", "quantity": 2,
                   "specialInstructions": "no salt", "fractionalQuantity": None, "modifierGroups": []}
    assert cart.create_cart_input("rx") == {"restaurantGuid": "rx", "orderSource": "ONLINE",
                                            "cartFulfillmentInput": {"fulfillmentType": "ASAP"},
                                            "digitalSurface": "OO_BASIC", "channelGuid": None}


def test_parse_when_and_snap():
    now = datetime(2026, 9, 6, 17, 0, tzinfo=timezone.utc)  # noon in Kansas City
    assert cart.parse_when("asap", now, TZ) is None
    assert cart.parse_when("12:30", now, TZ) == datetime(2026, 9, 6, 12, 30, tzinfo=TZ)
    assert cart.parse_when("6pm", now, TZ) == datetime(2026, 9, 6, 18, 0, tzinfo=TZ)
    assert cart.parse_when("11:00", now, TZ) == datetime(2026, 9, 7, 11, 0, tzinfo=TZ)  # already passed today
    assert cart.parse_when("tomorrow 12:15", now, TZ) == datetime(2026, 9, 7, 12, 15, tzinfo=TZ)
    assert cart.parse_when("+30m", now, TZ) == datetime(2026, 9, 6, 12, 30, tzinfo=TZ)
    with pytest.raises(toast.ToastError):
        cart.parse_when("noonish", now, TZ)
    slots = [now + timedelta(minutes=15 * i) for i in range(1, 6)]
    assert cart.snap_to_slot(now + timedelta(minutes=20), slots) == now + timedelta(minutes=30)
    with pytest.raises(toast.ToastError, match="Nearest offered"):
        cart.snap_to_slot(now + timedelta(hours=5), slots)


def test_summary_lists_lines_and_pickup():
    c = {"order": {"selections": [{"name": "Hot Dog", "price": 7, "quantity": 1, "modifiers": [{"name": "Grilled"}]}],
                   "preDiscountItemsSubtotal": 7, "taxV2": 0.84, "totalV2": 7.84},
         "fulfillmentType": "FUTURE", "fulfillmentDateTime": "2026-09-06T18:30:00+0000"}
    text = cart.summary(c, TZ)
    assert "1 x Hot Dog" in text and "Grilled" in text and "$7.84" in text and "Pickup: Sunday 1:30 PM" in text


def test_store_roundtrip(tmp_path):
    s = Store(tmp_path / "cfg")
    s.save_profile({"first": "A"})
    s.restaurant("slug")["cart"] = {"guid": "x"}
    s.save_state()
    s2 = Store(tmp_path / "cfg")
    assert s2.profile == {"first": "A"}
    assert s2.restaurant("slug") == {"cart": {"guid": "x"}}
    assert oct((tmp_path / "cfg" / "config.json").stat().st_mode & 0o777) == "0o600"


def test_oaep_is_well_formed():
    n, e = cardcrypto.parse_public_key(base64.b64decode(cardcrypto.PUBLIC_PEM_B64).decode())
    assert n.bit_length() == 2048 and e == 65537
    ct = cardcrypto.oaep_encrypt(b"hello", n, e, rand=b"\x01" * 20)
    assert len(ct) == 256
    assert ct == cardcrypto.oaep_encrypt(b"hello", n, e, rand=b"\x01" * 20)
    assert ct != cardcrypto.oaep_encrypt(b"hello", n, e)
    key_id, blob = cardcrypto.encrypt_card("4111111111111111", "09", "28", "123", "64112")
    assert key_id.startswith("RSA-OAEP-SHA1::") and len(base64.b64decode(blob)) == 256


def test_parse_toast_time_accepts_both_wire_formats():
    a = cart.parse_toast_time("2026-09-06T19:15:00+0000")
    b = cart.parse_toast_time("2026-09-06T19:15:00.000+00:00")
    assert a == b == datetime(2026, 9, 6, 19, 15, tzinfo=timezone.utc)


class FakeTransport:
    def __init__(self, data):
        self.data = data
        self.calls = []

    def query(self, op, variables, restaurant=True):
        self.calls.append((op, variables))
        return self.data

    def mutate(self, op, variables):
        self.calls.append((op, variables))
        return self.data

    def restaurant_guid(self):
        return "rx"

    def _log(self, *a):
        pass


def test_cart_get_injects_guid_and_maps_not_found():
    t = FakeTransport({"cartV2": {"__typename": "CartResponse", "cart": {"order": {"selections": []}}}})
    assert cart.get(t, "abc")["guid"] == "abc"
    t = FakeTransport({"cartV2": {"__typename": "CartError", "code": "CART_NOT_FOUND", "message": "gone"}})
    assert cart.get(t, "abc") is None
    t = FakeTransport({"cartV2": {"__typename": "CartError", "code": "OTHER", "message": "nope"}})
    with pytest.raises(cart.CartError, match="nope"):
        cart.get(t, "abc")


def test_add_unwraps_out_of_stock():
    t = FakeTransport({"addItemToCartV2": {"__typename": "CartOutOfStockError", "message": "sold out", "items": [{"name": "Pizza Puff"}]}})
    with pytest.raises(cart.CartError, match="Pizza Puff"):
        cart.add(t, None, {})
    assert t.calls[0][1]["input"]["createCartInput"]["restaurantGuid"] == "rx"


def test_place_order_uses_place_spi_order_shape():
    from dddcli import checkout
    done = {"placeOrder": {"__typename": "PlaceOrderResponse", "completedOrder": {"guid": "o1", "checkNumber": 7}}}
    t = FakeTransport(done)
    intent = {"id": "pi", "sessionSecret": "sec"}
    out = checkout.place_order(t, "cart1", {"firstName": "A"}, 2.5, intent, "pm1", fraud_session_id="f", intent_ref="ref")
    assert out["checkNumber"] == 7
    op, variables = t.calls[0]
    assert op == "PlaceSpiOrder"
    spi = variables["input"]["spiPaymentData"]
    assert spi == {"paymentIntentId": "ref", "paymentMethodId": "pm1", "sessionSecret": "sec", "saveCard": False, "ccFraudSessionId": "f"}
    assert variables["input"]["tipAmount"] == 2.5 and variables["input"]["cartGuid"] == "cart1"
    t = FakeTransport({"placeOrder": {"__typename": "PlaceOrderError", "placeOrderErrorCode": "PAYMENT_FAILED", "message": "declined"}})
    with pytest.raises(checkout.PaymentError, match="declined"):
        checkout.place_order(t, "cart1", {}, 0, intent, "pm1")


def test_selection_input_scales_modifier_quantity_with_the_item():
    it = menu.find(menu.flatten(MENUS), "hot dog")
    mods = [{"guid": "mg1", "modifiers": [{"itemGuid": "c1", "itemGroupGuid": None, "quantity": 1, "modifierGroups": []}]}]
    sel = cart.selection_input(it, 2, mods, None)
    assert sel["quantity"] == 2
    assert sel["modifierGroups"][0]["modifiers"][0]["quantity"] == 2


def test_card_from_env(monkeypatch):
    from dddcli import checkout
    monkeypatch.delenv("DDD_CARD_NUMBER", raising=False)
    assert checkout.card_from_env() is None
    monkeypatch.setenv("DDD_CARD_NUMBER", "4111 1111 1111 1111")
    monkeypatch.setenv("DDD_CARD_EXP", "11/30")
    monkeypatch.setenv("DDD_CARD_CVV", "165")
    monkeypatch.setenv("DDD_CARD_ZIP", "64112")
    card = checkout.card_from_env()
    assert card.last4 == "1111" and card.exp_month == "11" and card.exp_year == "30" and card.zip_code == "64112"
    monkeypatch.setenv("DDD_CARD_NUMBER", "1234")
    with pytest.raises(checkout.PaymentError, match="DDD_CARD_"):
        checkout.card_from_env()


def test_update_intent_is_skipped_when_it_would_change_nothing():
    from dddcli import checkout
    # spiCreatePaymentIntent already returns the tax-inclusive total, so a tipless
    # order has nothing to update and the web app sends no update call.
    intent = {"id": "pi", "sessionSecret": "sec", "amount": 448}
    t = FakeTransport({})
    assert checkout.update_intent_if_needed(t, "cart1", intent, "a@b.c", 0, 0.48, 4.48) is None
    assert t.calls == []
    # A tip changes the amount, so the update still goes out.
    ok = {"oo": {"spiUpdatePaymentIntent": {"__typename": "OnlineOrderingSpiUpdatePaymentIntentSuccessResponse"}}}
    t = FakeTransport(ok)
    checkout.update_intent_if_needed(t, "cart1", intent, "a@b.c", 1.0, 0.48, 5.48)
    assert t.calls[0][0] == "UpdatePaymentIntent"
    # So does a mismatch between the intent and the cart, tip or no tip.
    t = FakeTransport(ok)
    checkout.update_intent_if_needed(t, "cart1", intent, "a@b.c", 0, 0.48, 9.99)
    assert t.calls[0][0] == "UpdatePaymentIntent"


def test_card_fields_survive_bracketed_paste():
    from dddcli import checkout
    # getpass reads the tty raw, so a value pasted from a password manager arrives
    # wrapped in ESC[200~ ... ESC[201~. Those markers carry digits (200, 201) that used
    # to survive the digits-only filter and blow the length check.
    visa = "4111111111111111"  # documented test number, not anyone's card
    card = checkout.normalize_card(
        f"\x1b[200~{visa}\x1b[201~", "\x1b[200~11/30\x1b[201~", "\x1b[200~123\x1b[201~", " 64111 ", "Alex Hedtke"
    )
    assert card.number == visa
    assert (card.exp_month, card.exp_year, card.cvv, card.zip_code) == ("11", "30", "123", "64111")
    # A genuinely bad number must still be refused.
    with pytest.raises(checkout.PaymentError, match="valid card number"):
        checkout.normalize_card("4111111111111112", "11/30", "123", "64111", None)


class SequenceTransport(FakeTransport):
    """Returns a different response per call, so retry behaviour can be exercised."""

    def __init__(self, responses):
        super().__init__(None)
        self.responses = list(responses)

    def mutate(self, op, variables):
        self.calls.append((op, variables))
        return self.responses.pop(0)


CRASH = {"placeOrder": {"__typename": "PlaceOrderError", "placeOrderErrorCode": "CRITICAL_ERROR", "message": "unknown error"}}
DONE = {"placeOrder": {"__typename": "PlaceOrderResponse", "completedOrder": {"guid": "o1", "checkNumber": 42}}}


def test_place_retries_only_the_server_crash(monkeypatch):
    from dddcli import checkout
    monkeypatch.setattr(checkout.time, "sleep", lambda _s: None)
    intent = {"id": "pi", "sessionSecret": "sec"}

    # A crash is an unhandled server exception, so it is worth waiting out. Retrying costs
    # nothing because /confirm already made the authorization and placing adds no other.
    t = SequenceTransport([CRASH, CRASH, DONE])
    out = checkout.place_order_with_retry(t, "cart1", {}, 0, intent, "pm1", attempts=5)
    assert out["checkNumber"] == 42
    assert len(t.calls) == 3

    # A refusal is a real answer. Do not hammer Toast with it.
    declined = {"placeOrder": {"__typename": "PlaceOrderError", "placeOrderErrorCode": "PAYMENT_FAILED", "message": "declined"}}
    t = SequenceTransport([declined, DONE])
    with pytest.raises(checkout.PaymentError, match="declined"):
        checkout.place_order_with_retry(t, "cart1", {}, 0, intent, "pm1", attempts=5)
    assert len(t.calls) == 1

    # Exhausting the attempts surfaces the crash rather than swallowing it.
    t = SequenceTransport([CRASH, CRASH])
    with pytest.raises(checkout.PlaceCrashed, match="CRITICAL_ERROR"):
        checkout.place_order_with_retry(t, "cart1", {}, 0, intent, "pm1", attempts=2)
    assert len(t.calls) == 2
