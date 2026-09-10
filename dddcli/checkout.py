"""Paying for a cart and placing the order.

Toast's online ordering has two card flows, and the restaurant page says which one its
web app runs (window.__FLAGS_STATE__["oo-server-spi"], read through Transport.flags()).

Server SPI (flag on; Ding Dong Dogs, read from the live bundle 2026-09-10):

1. spiCreatePaymentIntent (GraphQL) for the cart -> intent id + sessionSecret. The web
   app also attaches a reCAPTCHA Enterprise token; the gateway accepts the call without.
2. spiGetClientToken -> a JWT the hosted checkout iframe uses as its bearer token.
3. The iframe encrypts the card (see cardcrypto) and POSTs it to
   payments.toasttab.com/v1/payment-methods with that JWT. This module makes the same
   call. Nothing is confirmed client-side.
4. placeSpiOrder (GraphQL) with the intent id, the payment method id and the session
   secret. Toast confirms (authorizes) and captures the payment and creates the order in
   one step. There is no spiUpdatePaymentIntent in this flow; the tip rides on the order.

Client SPI (flag off): steps 1 to 3, then spiUpdatePaymentIntent with the tip and tax in
cents, then POST /v1/payment-intents/{id}/confirm (which authorizes the card), then
placePaidOrder carrying the confirmed payment's externalReferenceId as paymentId, which
Toast captures.

Mixing the two, confirming client-side and then calling placeSpiOrder, asks Toast to
confirm an intent that is already confirmed. It answers with an unhandled CRITICAL_ERROR
and creates no order, which is what earlier versions of this tool did.

Card details are read from the OS credential store (macOS Keychain or Windows Credential
Manager) or typed at a prompt, live in memory for the duration of the command, and are
never written to disk by this tool.
"""

from __future__ import annotations

import getpass
import json
import re
import platform
import subprocess
import time
from collections.abc import Callable
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


class PlaceCrashed(PaymentError):
    """placeSpiOrder threw CRITICAL_ERROR, an unhandled exception rather than a refusal."""


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


# getpass reads the tty raw, without readline, so a value pasted from a password manager
# arrives wrapped in bracketed-paste markers (ESC[200~ ... ESC[201~). The digits in those
# markers survive a digits-only filter and push a good 16-digit number out to 22, which
# then fails the length check as "not a valid card number".
_ESCAPES = re.compile(r"\x1b\[[0-9;]*[~a-zA-Z]|\x1b.|[\x00-\x1f\x7f]")


def _clean(text: str) -> str:
    """Drop terminal escape sequences and control characters from a typed or pasted field."""
    return _ESCAPES.sub("", text or "").strip()


def normalize_card(number: str, exp: str, cvv: str, zip_code: str, name: str | None) -> Card:
    number, exp, cvv, zip_code, name = (_clean(number), _clean(exp), _clean(cvv), _clean(zip_code), _clean(name or ""))
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


# ---- card storage --------------------------------------------------------------
#
# macOS: the login Keychain, through the `security` tool (a generic password item).
# Windows: the Credential Manager, through advapi32's CredWrite/CredRead/CredDelete
# (a generic credential, persisted for this user on this machine). Both hold the card
# as one JSON blob under the same name. Elsewhere there is no store: checkout prompts.

CRED_TARGET = KEYCHAIN_SERVICE
CRED_COMMENT = "Ding Dong Dogs CLI card"
_CRED_TYPE_GENERIC = 1
_CRED_PERSIST_LOCAL_MACHINE = 2
_ERROR_NOT_FOUND = 1168


def card_store() -> str | None:
    """Name of the card store on this OS, or None when checkout has to ask for the card."""
    system = platform.system()
    if system == "Darwin":
        return "macOS Keychain"
    if system == "Windows":
        return "Windows Credential Manager"
    return None


def save_card(card: Card) -> None:
    store = card_store()
    if store is None:
        raise PaymentError("No card store on this OS (macOS Keychain or Windows Credential Manager); the card is asked for at checkout.")
    if store == "Windows Credential Manager":
        _win_cred_write(CRED_TARGET, KEYCHAIN_ACCOUNT, card.to_json())
        return
    subprocess.run(
        ["security", "add-generic-password", "-a", KEYCHAIN_ACCOUNT, "-s", KEYCHAIN_SERVICE, "-l", CRED_COMMENT,
         "-U", "-w", card.to_json()],
        check=True, capture_output=True,
    )


def load_card() -> Card | None:
    store = card_store()
    if store == "Windows Credential Manager":
        text = _win_cred_read(CRED_TARGET)
        return Card.from_json(text) if text else None
    if store == "macOS Keychain":
        r = subprocess.run(["security", "find-generic-password", "-a", KEYCHAIN_ACCOUNT, "-s", KEYCHAIN_SERVICE, "-w"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            return None
        return Card.from_json(r.stdout.strip())
    return None


def clear_card() -> bool:
    store = card_store()
    if store == "Windows Credential Manager":
        return _win_cred_delete(CRED_TARGET)
    if store == "macOS Keychain":
        r = subprocess.run(["security", "delete-generic-password", "-a", KEYCHAIN_ACCOUNT, "-s", KEYCHAIN_SERVICE],
                           capture_output=True)
        return r.returncode == 0
    return False


def _win_credential_struct():
    """CREDENTIALW from wincred.h, in fixed-width types so the layout is the same wherever
    it is built (DWORD is 32 bits on Windows; ctypes.c_ulong is not on other platforms).
    Built on demand so the module imports on every OS."""
    import ctypes

    class FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", ctypes.c_uint32), ("dwHighDateTime", ctypes.c_uint32)]

    class CREDENTIAL(ctypes.Structure):
        _fields_ = [
            ("Flags", ctypes.c_uint32),
            ("Type", ctypes.c_uint32),
            ("TargetName", ctypes.c_wchar_p),
            ("Comment", ctypes.c_wchar_p),
            ("LastWritten", FILETIME),
            ("CredentialBlobSize", ctypes.c_uint32),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_char)),
            ("Persist", ctypes.c_uint32),
            ("AttributeCount", ctypes.c_uint32),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", ctypes.c_wchar_p),
            ("UserName", ctypes.c_wchar_p),
        ]

    return CREDENTIAL


def _advapi32(credential_type):
    import ctypes

    api = ctypes.WinDLL("advapi32", use_last_error=True)  # type: ignore[attr-defined]
    api.CredWriteW.argtypes = [ctypes.POINTER(credential_type), ctypes.c_uint32]
    api.CredWriteW.restype = ctypes.c_int
    api.CredReadW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.POINTER(ctypes.POINTER(credential_type))]
    api.CredReadW.restype = ctypes.c_int
    api.CredDeleteW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32]
    api.CredDeleteW.restype = ctypes.c_int
    api.CredFree.argtypes = [ctypes.c_void_p]
    api.CredFree.restype = None
    return api


def _win_cred_write(target: str, user: str, secret: str) -> None:
    import ctypes

    credential_type = _win_credential_struct()
    api = _advapi32(credential_type)
    blob = secret.encode("utf-8")
    buf = ctypes.create_string_buffer(blob, len(blob))
    cred = credential_type()
    cred.Type = _CRED_TYPE_GENERIC
    cred.TargetName = target
    cred.Comment = CRED_COMMENT
    cred.CredentialBlobSize = len(blob)
    cred.CredentialBlob = ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))
    cred.Persist = _CRED_PERSIST_LOCAL_MACHINE
    cred.UserName = user
    if not api.CredWriteW(ctypes.byref(cred), 0):
        raise PaymentError(f"Windows Credential Manager refused the card (error {ctypes.get_last_error()}).")


def _win_cred_read(target: str) -> str | None:
    import ctypes

    credential_type = _win_credential_struct()
    api = _advapi32(credential_type)
    pcred = ctypes.POINTER(credential_type)()
    if not api.CredReadW(target, _CRED_TYPE_GENERIC, 0, ctypes.byref(pcred)):
        err = ctypes.get_last_error()
        if err == _ERROR_NOT_FOUND:
            return None
        raise PaymentError(f"Windows Credential Manager could not read the card (error {err}).")
    try:
        cred = pcred.contents
        return ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize).decode("utf-8")
    finally:
        api.CredFree(pcred)


def _win_cred_delete(target: str) -> bool:
    credential_type = _win_credential_struct()
    return bool(_advapi32(credential_type).CredDeleteW(target, _CRED_TYPE_GENERIC, 0))


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
    print("Card details (kept in memory only; `ddd card set` stores them in the OS credential store).")
    number = getpass.getpass("Card number: ")
    exp = input("Expiry (MM/YY): ")
    cvv = getpass.getpass("CVV: ")
    zip_code = input("Billing ZIP: ")
    name = input(f"Name on card [{name_default or ''}]: ") or name_default
    return normalize_card(number, exp, cvv, zip_code, name)


# ---- Toast calls ---------------------------------------------------------------

FLOW_SERVER = "server-spi"
FLOW_CLIENT = "client-spi"


def flow(t: Transport) -> str:
    """Which card flow this restaurant runs (see the module docstring).

    The web app decides with two values the restaurant page bootstraps: the country
    (non-US restaurants pay through Adyen and a different mutation, not supported here)
    and the oo-server-spi flag.
    """
    country = t.country() or "US"
    if country != "US":
        raise PaymentError(f"This restaurant is in {country} and pays through Adyen, which ddd does not support.")
    return FLOW_SERVER if t.flags().get("oo-server-spi") else FLOW_CLIENT


def surcharging(t: Transport) -> bool:
    """With oo-spi-surcharging-fe on, the web app threads the payment method id through
    the intent update and, in the client flow, sends the update's surchargeAmount on the
    order. The Cart query carries no surcharge amounts, so in the server flow the amount
    is omitted, as the web app omits it for a restaurant with none."""
    return bool(t.flags().get("oo-spi-surcharging-fe"))


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


def create_intent(t: Transport, cart_guid: str, session_id: str) -> dict:
    """`session_id` is the web app's Sift session id: one per checkout, sent here and again
    as the order's ccFraudSessionId."""
    data = t.mutate(
        "CreatePaymentIntent",
        {"input": {
            "cartGuid": cart_guid,
            "paymentMethodConfigId": PAYMENT_METHOD_CONFIG_ID,
            "orderSource": "ONLINE",
            "deliveryProvider": None,
            "sessionId": session_id,
            "reCaptchaToken": None,
        }},
    )
    return _unwrap((data.get("oo") or {}).get("spiCreatePaymentIntent"), "OnlineOrderingSpiCreatePaymentIntentSuccessResponse", "payment intent")


def update_intent(t: Transport, cart_guid: str, intent: dict, email: str, tip: float, tax: float,
                  payment_method_id: str | None = None) -> dict:
    """Client flow only. The web app always sends this before confirming, tip or no tip."""
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
            "paymentMethodId": payment_method_id,
            "amountDetails": {"tip": int(round(tip * 100)), "tax": {"totalTaxAmount": int(round(tax * 100))}},
        }},
    )
    return _unwrap((data.get("oo") or {}).get("spiUpdatePaymentIntent"), "OnlineOrderingSpiUpdatePaymentIntentSuccessResponse", "payment intent update")


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
    """Tokenize the card the way the hosted iframe does. This neither authorizes nor charges."""
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
    """Client flow only: authorizes the card (a pending hold appears). Never call this in
    the server flow; placeSpiOrder does it and crashes on an intent confirmed twice."""
    body = {
        "sessionSecret": intent["sessionSecret"],
        "paymentMethodData": {"scope": "SINGLE_USE", "type": "CARD"},
        "paymentMethodId": payment_method_id,
        "setupFutureUsage": None,
        "email": email,
    }
    return _payments_post(t, token, intent["id"], f"v1/payment-intents/{intent['id']}/confirm", body)


def _order_input(cart_guid: str, customer: dict, tip: float) -> dict:
    return {
        "cartGuid": cart_guid,
        "customer": customer,
        "digitalSurface": "OO_BASIC",
        "isCustomDomain": False,
        "tipAmount": round(tip, 2),
        "deliveryCommunicationConsentGiven": True,
    }


def _placed(data: dict, op: str) -> dict:
    resp = data.get("placeOrder") or {}
    kind = resp.get("__typename")
    if kind == "PlaceOrderResponse":
        return resp["completedOrder"]
    if kind == "PlaceOrderCartUpdatedError":
        raise PaymentError(f"Toast changed the cart while ordering ({resp.get('placeOrderCartUpdatedErrorCode')}): {resp.get('message')}. Check `ddd cart` and try again.")
    code = resp.get("placeOrderErrorCode") or kind
    if code == "CRITICAL_ERROR":
        raise PlaceCrashed(f"{op}: order not placed ({code}): {resp.get('message') or resp}")
    raise PaymentError(f"{op}: order not placed ({code}): {resp.get('message') or resp}")


def place_spi_order(t: Transport, cart_guid: str, customer: dict, tip: float, intent: dict,
                    payment_method_id: str, fraud_session_id: str) -> dict:
    """Server flow: hand Toast the unconfirmed intent and the tokenized card; it confirms,
    captures and creates the order in one step. Nothing is authorized before this call,
    so a refusal here leaves no hold."""
    inp = _order_input(cart_guid, customer, tip)
    inp["spiPaymentData"] = {
        "paymentIntentId": intent["id"],
        "paymentMethodId": payment_method_id,
        "sessionSecret": intent["sessionSecret"],
        "saveCard": False,
        "ccFraudSessionId": fraud_session_id,
    }
    return _placed(t.mutate("PlaceSpiOrder", {"input": inp}), "placeSpiOrder")


def place_paid_order(t: Transport, cart_guid: str, customer: dict, tip: float, payment_id: str,
                     payment_method_id: str | None = None, surcharge_amount: float | None = None,
                     with_surcharging: bool = False) -> dict:
    """Client flow: the card is already authorized; Toast captures the payment the confirmed
    payment's externalReferenceId names. With surcharging on, the web app also sends the
    payment method id and the intent update's surchargeAmount (null when there is none)."""
    inp = _order_input(cart_guid, customer, tip)
    inp["paymentId"] = payment_id
    if with_surcharging:
        inp["paymentMethodId"] = payment_method_id
        inp["surchargeAmount"] = surcharge_amount
    return _placed(t.mutate("PlacePaidOrder", {"input": inp}), "placePaidOrder")


def place_with_retry(t: Transport, place: Callable[[], dict], attempts: int = 3, delay: float = 3.0) -> dict:
    """Run a placement, retrying only Toast's unhandled CRITICAL_ERROR.

    A retry does not authorize again: the authorization belongs to the payment intent and
    Toast will not confirm or capture the same intent twice (observed live: repeated
    placements against one confirmed intent produced a single authorization). A graceful refusal,
    a declined card or a cart changed underneath, is a real answer and is raised at once.
    """
    last: PlaceCrashed | None = None
    for n in range(1, attempts + 1):
        try:
            return place()
        except PlaceCrashed as e:
            last = e
            t._log(f"place attempt {n}/{attempts} crashed; " + (f"retrying in {delay:.0f}s" if n < attempts else "giving up"))  # noqa: SLF001
            if n < attempts:
                time.sleep(delay)
    raise last  # type: ignore[misc]


def completed_order(t: Transport, order_guid: str) -> dict:
    data = t.query("CompletedOrder", {"input": {"orderGuid": order_guid, "restaurantGuid": t.restaurant_guid()}})
    return data.get("completedOrder") or {}
