"""Paying for a cart and placing the order.

Toast's online ordering pays through its "SPI" flow (verified live 2026-09-06):

1. spiCreatePaymentIntent (GraphQL) for the cart -> intent id + sessionSecret.
   The web app also attaches a reCAPTCHA Enterprise token here; the gateway accepts
   the call without one.
2. spiUpdatePaymentIntent with the tip and tax breakdown.
3. spiGetClientToken -> a JWT the hosted checkout iframe uses as its bearer token.
4. The iframe encrypts the card (see cardcrypto) and POSTs it to
   payments.toasttab.com/v1/payment-methods, then POSTs
   /v1/payment-intents/{id}/confirm. Both authorized with that JWT plus the
   restaurant guid. This module makes the same two calls.
5. placeSpiOrder (GraphQL) with the intent id, payment method id and session secret
   captures the authorized payment and creates the order. (placePaidOrder is the
   sibling for payments the client already captured; its input has no spiPaymentData.)

Card details are read from the macOS Keychain or typed at a prompt, live in memory
for the duration of the command, and are never written to disk by this tool.
"""

from __future__ import annotations

import getpass
import json
import platform
import subprocess
import uuid
from dataclasses import dataclass

from dddcli import cardcrypto
from dddcli.toast import ToastError, Transport

PAYMENTS_HOST = "https://payments.toasttab.com"
# Static id from the ordering app's config (resources.paymentMethodConfigId in the web bundle).
PAYMENT_METHOD_CONFIG_ID = "874631f2-eb32-4287-8c6b-d5c0eb6bbff6"
KEYCHAIN_SERVICE = "ddd-card"
KEYCHAIN_ACCOUNT = "ddd"


class PaymentError(ToastError):
    pass


@dataclass
class Card:
    number: str
    exp_month: str
    exp_year: str
    cvv: str
    zip_code: str
    name: str | None = None

    @property
    def last4(self) -> str:
        return self.number[-4:]

    @property
    def label(self) -> str:
        return f"card ending {self.last4} (exp {self.exp_month}/{self.exp_year})"

    def to_json(self) -> str:
        return json.dumps({"number": self.number, "exp_month": self.exp_month, "exp_year": self.exp_year,
                           "cvv": self.cvv, "zip_code": self.zip_code, "name": self.name})

    @classmethod
    def from_json(cls, text: str) -> "Card":
        d = json.loads(text)
        return cls(d["number"], d["exp_month"], d["exp_year"], d["cvv"], d["zip_code"], d.get("name"))


def normalize_card(number: str, exp: str, cvv: str, zip_code: str, name: str | None) -> Card:
    digits = "".join(ch for ch in number if ch.isdigit())
    if not 13 <= len(digits) <= 19 or not _luhn(digits):
        raise PaymentError("That does not look like a valid card number.")
    e = "".join(ch for ch in exp if ch.isdigit())
    if len(e) == 6:
        e = e[:2] + e[4:]
    if len(e) != 4 or not 1 <= int(e[:2]) <= 12:
        raise PaymentError("Expiry must be MM/YY.")
    c = "".join(ch for ch in cvv if ch.isdigit())
    if len(c) not in (3, 4):
        raise PaymentError("CVV must be 3 or 4 digits.")
    z = "".join(ch for ch in zip_code if ch.isalnum() or ch in "- ").strip()
    if not z:
        raise PaymentError("Billing ZIP is required.")
    return Card(digits, e[:2], e[2:], c, z, (name or "").strip() or None)


def _luhn(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


# ---- card storage (macOS Keychain) ---------------------------------------------

def keychain_available() -> bool:
    return platform.system() == "Darwin"


def save_card(card: Card) -> None:
    if not keychain_available():
        raise PaymentError("Card storage uses the macOS Keychain; on this OS the card is asked for at checkout.")
    subprocess.run(
        ["security", "add-generic-password", "-a", KEYCHAIN_ACCOUNT, "-s", KEYCHAIN_SERVICE, "-l", "Ding Dong Dogs CLI card",
         "-U", "-w", card.to_json()],
        check=True, capture_output=True,
    )


def load_card() -> Card | None:
    if not keychain_available():
        return None
    r = subprocess.run(["security", "find-generic-password", "-a", KEYCHAIN_ACCOUNT, "-s", KEYCHAIN_SERVICE, "-w"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return None
    return Card.from_json(r.stdout.strip())


def clear_card() -> bool:
    if not keychain_available():
        return False
    r = subprocess.run(["security", "delete-generic-password", "-a", KEYCHAIN_ACCOUNT, "-s", KEYCHAIN_SERVICE],
                       capture_output=True)
    return r.returncode == 0


def card_from_env() -> Card | None:
    """DDD_CARD_NUMBER, DDD_CARD_EXP (MM/YY), DDD_CARD_CVV, DDD_CARD_ZIP, optional DDD_CARD_NAME.
    For scripts and agents that cannot answer a prompt; the process environment is the only
    place the card exists."""
    import os
    number = os.environ.get("DDD_CARD_NUMBER")
    if not number:
        return None
    try:
        return normalize_card(number, os.environ.get("DDD_CARD_EXP", ""), os.environ.get("DDD_CARD_CVV", ""),
                              os.environ.get("DDD_CARD_ZIP", ""), os.environ.get("DDD_CARD_NAME"))
    except PaymentError as e:
        raise PaymentError(f"DDD_CARD_* environment: {e}") from None


def prompt_card(name_default: str | None = None) -> Card:
    print("Card details (kept in memory only; `ddd card set` stores them in the Keychain).")
    number = getpass.getpass("Card number: ")
    exp = input("Expiry (MM/YY): ")
    cvv = getpass.getpass("CVV: ")
    zip_code = input("Billing ZIP: ")
    name = input(f"Name on card [{name_default or ''}]: ") or name_default
    return normalize_card(number, exp, cvv, zip_code, name)


# ---- Toast calls ---------------------------------------------------------------

def _unwrap(resp: dict | None, ok_type: str, what: str) -> dict:
    if not resp:
        raise PaymentError(f"{what}: empty response")
    if resp.get("__typename") != ok_type:
        raise PaymentError(f"{what}: {resp.get('message') or resp.get('__typename')}")
    return resp


def client_token(t: Transport) -> str:
    data = t.query("GetClientToken", {})
    resp = _unwrap((data.get("oo") or {}).get("spiGetClientToken"), "OnlineOrderingSpiGetClientTokenSuccessResponse", "client token")
    return resp["token"]


def create_intent(t: Transport, cart_guid: str) -> dict:
    data = t.mutate(
        "CreatePaymentIntent",
        {"input": {
            "cartGuid": cart_guid,
            "paymentMethodConfigId": PAYMENT_METHOD_CONFIG_ID,
            "orderSource": "ONLINE",
            "deliveryProvider": None,
            "sessionId": str(uuid.uuid4()),
            "reCaptchaToken": None,
        }},
    )
    return _unwrap((data.get("oo") or {}).get("spiCreatePaymentIntent"), "OnlineOrderingSpiCreatePaymentIntentSuccessResponse", "payment intent")


def update_intent(t: Transport, cart_guid: str, intent: dict, email: str, tip: float, tax: float) -> dict:
    data = t.mutate(
        "UpdatePaymentIntent",
        {"input": {
            "cartGuid": cart_guid,
            "email": email,
            "globalGiftCardPaymentAmount": 0,
            "paymentIntentId": intent["id"],
            "restaurantGiftCardPaymentAmount": 0,
            "tipAmount": round(tip, 2),
            "fundraisingAmount": 0,
            "enableConfirmRetry": False,
            "paymentMethodId": None,
            "amountDetails": {"tip": int(round(tip * 100)), "tax": {"totalTaxAmount": int(round(tax * 100))}},
        }},
    )
    return _unwrap((data.get("oo") or {}).get("spiUpdatePaymentIntent"), "OnlineOrderingSpiUpdatePaymentIntentSuccessResponse", "payment intent update")


def update_intent_if_needed(t: Transport, cart_guid: str, intent: dict, email: str, tip: float,
                            tax: float, total: float) -> dict | None:
    """Update the intent only when that actually changes the amount, as the site does.

    spiCreatePaymentIntent already returns the tax-inclusive cart total (verified live
    2026-09-08: a $4.00 cart carrying $0.48 tax creates an intent with amount 448), so
    with no tip there is nothing left to set and the web app makes no update call at all.
    Sending a redundant one is a suspect in the placeSpiOrder capture failure. Whenever
    the amounts disagree the update still goes out, so this cannot under-authorize.
    """
    want = int(round(total * 100))
    if not tip and intent.get("amount") == want:
        return None
    return update_intent(t, cart_guid, intent, email, tip, tax)


def _payments_post(t: Transport, token: str, intent_id: str | None, path: str, body: dict) -> dict:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
        "Toast-Restaurant-External-ID": t.restaurant_guid(),
        "Origin": PAYMENTS_HOST,
        "Referer": PAYMENTS_HOST + "/assets/",
    }
    if intent_id:
        headers["Toast-HC-Correlation-ID"] = intent_id
    t._log("POST", path, json.dumps({k: v for k, v in body.items() if k != "card"})[:300])  # noqa: SLF001
    r = t.http.post(f"{PAYMENTS_HOST}/{path}", json=body, headers=headers, timeout=60)
    try:
        payload = r.json()
    except ValueError:
        raise PaymentError(f"{path}: HTTP {r.status_code}: {r.text[:200]}") from None
    if r.status_code >= 300:
        msg = payload.get("message") or payload.get("developerMessage") or str(payload)[:200]
        errs = payload.get("errors") or []
        if errs:
            msg += " (" + "; ".join(str(e.get("message") or e) for e in errs) + ")"
        raise PaymentError(f"{path}: {msg}")
    return payload


def create_payment_method(t: Transport, token: str, intent: dict, card: Card, email: str) -> dict:
    key_id, blob = cardcrypto.encrypt_card(card.number, card.exp_month, card.exp_year, card.cvv, card.zip_code, card.name)
    body = {
        "type": "CARD",
        "card": {"keyId": key_id, "cardData": blob},
        "sessionSecret": intent["sessionSecret"],
        "usage": None,
        "setupFutureUsage": None,
        "billingDetails": {"email": email, "name": card.name} if card.name else {"email": email},
    }
    return _payments_post(t, token, intent["id"], "v1/payment-methods", body)


def confirm_payment(t: Transport, token: str, intent: dict, payment_method_id: str, email: str) -> dict:
    body = {
        "sessionSecret": intent["sessionSecret"],
        "paymentMethodData": {"scope": "SINGLE_USE", "type": "CARD"},
        "paymentMethodId": payment_method_id,
        "setupFutureUsage": None,
        "email": email,
    }
    return _payments_post(t, token, intent["id"], f"v1/payment-intents/{intent['id']}/confirm", body)


def place_order(t: Transport, cart_guid: str, customer: dict, tip: float, intent: dict, payment_method_id: str,
                fraud_session_id: str | None = None, intent_ref: str | None = None) -> dict:
    """Place the order against an authorized payment. `intent_ref` is what the web app passes
    as paymentIntentId after a confirm (the confirmed payment's externalReferenceId); it
    defaults to the intent id."""
    data = t.mutate(
        "PlaceSpiOrder",
        {"input": {
            "cartGuid": cart_guid,
            "customer": customer,
            "digitalSurface": "OO_BASIC",
            "isCustomDomain": False,
            "tipAmount": round(tip, 2),
            "deliveryCommunicationConsentGiven": True,
            "spiPaymentData": {
                "paymentIntentId": intent_ref or intent["id"],
                "paymentMethodId": payment_method_id,
                "sessionSecret": intent["sessionSecret"],
                "saveCard": False,
                "ccFraudSessionId": fraud_session_id or str(uuid.uuid4()),
            },
        }},
    )
    resp = data.get("placeOrder") or {}
    kind = resp.get("__typename")
    if kind == "PlaceOrderResponse":
        return resp["completedOrder"]
    if kind == "PlaceOrderCartUpdatedError":
        raise PaymentError(f"Toast changed the cart while ordering ({resp.get('placeOrderCartUpdatedErrorCode')}): {resp.get('message')}. Check `ddd cart` and try again.")
    raise PaymentError(f"Order not placed ({resp.get('placeOrderErrorCode') or kind}): {resp.get('message') or resp}")


def completed_order(t: Transport, order_guid: str) -> dict:
    data = t.query("CompletedOrder", {"input": {"orderGuid": order_guid, "restaurantGuid": t.restaurant_guid()}})
    return data.get("completedOrder") or {}
