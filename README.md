# ding-dong-dogs-cli

Order takeout from [Ding Dong Dogs](https://www.dingdongdogs.com/) (320 E 51st St, Kansas
City, across from UMKC) without opening a browser. `ddd` talks to the same Toast
online-ordering API their order page uses: browse the menu, build a cart with toppings and
upgrades, pick a pickup time, pay by card, and read the order back.

```
$ ddd add hot dog -m grilled -m "chili, diced onion" -m impossible
Added 1 x Hot Dog with Cook Type: Grilled; Toppings: Chili (+$1.00); Toppings: Diced Onion; Upgrade to Impossible Dog: Impossible Dog (+$2.00)
 1. 1 x Hot Dog                        $10.00
      Grilled, Chili, Diced Onion, Impossible Dog
    Subtotal                           $10.00
    Tax                                 $1.20
    Total                              $11.20
    Pickup: ASAP (about 15 min)
$ ddd checkout --tip-pct 20
```

The Toast layer is generic. `-r <slug>` (or `DDD_RESTAURANT`) points it at any restaurant
that orders through `order.toasttab.com/online/<slug>`; the default slug is Ding Dong Dogs.

## Install

Python 3.11+ and one dependency, [curl_cffi](https://github.com/lexiforest/curl_cffi)
(Cloudflare in front of Toast rejects every non-browser TLS fingerprint, and curl_cffi's
Chrome impersonation is what gets through).

```sh
git clone https://github.com/BaesTheorem/ding-dong-dogs-cli
cd ding-dong-dogs-cli
uv venv && uv pip install -e .      # or: python3 -m venv .venv && .venv/bin/pip install -e .
bin/ddd hours                        # bin/ddd prefers the repo venv; `ddd` is on PATH after pip install
```

## Commands

```sh
ddd hours                          # today's takeout hours, whether ASAP is on, next pickup slots
ddd menu                           # everything, grouped; --all includes out-of-stock, -g dogs for one group
ddd search tots                    # items by word
ddd item hot dog                   # one item with every choice, price adjustment, required/optional

ddd add tater tots                 # no choices needed
ddd add hot dog -m grilled         # required "Cook Type" choice by bare name
ddd add hot dog -m "Cook Type=Fried" -m "Toppings=Chili,Diced Onion" -m "Upgrade to Impossible Dog=Impossible Dog"
ddd add chili dog -n 2 --note "extra napkins"
ddd cart
ddd remove 2                       # by line number or name
ddd clear

ddd pickup                         # ASAP availability and the offered slots
ddd pickup 12:30                   # snaps to the nearest offered 15-minute slot
ddd pickup tomorrow 6pm
ddd pickup +45m
ddd pickup asap

ddd profile --first Alex --last H --email you@example.com --phone 8165551234 --tip-pct 20
ddd card set                       # card into the macOS Keychain (optional; checkout asks otherwise)
ddd checkout                       # shows the total, asks, charges, prints the check number
ddd checkout --tip 3 --yes         # no prompt
ddd checkout --dry-run             # validate with Toast, charge nothing
ddd order                          # the last order (or `ddd order <guid>`)

ddd refresh                        # re-read Toast's operation hashes after they redeploy
ddd raw Cart '{"guid": "...", "totalGiftCardBalance": 0}'      # any persisted operation; --mutation for POST
```

`--json` on any command prints Toast's records instead of text.

Choices (`-m`) match case-insensitively by exact name, then prefix, then substring, and
must be unambiguous. A group the user does not mention gets Toast's own default (Yellow
Mustard on a Hot Dog, say), the same as the website. Required groups without a default
fail with the list of choices.

## Where things live

- `~/.config/ddd/config.json` (0600): name, email, phone, default tip percent.
- `~/.config/ddd/state.json`: the open cart's guid, the session token, cached operation
  hashes. `DDD_CONFIG_DIR` moves both.
- Card details go into the macOS Keychain (`ddd card set`, item "ddd-card"), come from
  `DDD_CARD_NUMBER`, `DDD_CARD_EXP` (MM/YY), `DDD_CARD_CVV`, `DDD_CARD_ZIP` and optional
  `DDD_CARD_NAME` in the environment (for scripts), or are typed at checkout. They are encrypted in memory for the payment call and never written by this
  tool. On other platforms there is no storage; checkout prompts.

## Status

Verified live against Ding Dong Dogs on 2026-09-06: hours, menu, item choices, add with
modifiers and notes, remove, clear, ASAP and scheduled pickup, pre-checkout validation,
payment intent creation and tip update, and card tokenization (a Visa test number was
accepted by Toast's payments API and returned a payment method id). The last two steps of
checkout, confirming the payment and `placeSpiOrder`, are written from the ordering app's
own code but have not been run with a real card yet, so a live checkout is the test.
If confirm fails the cart is untouched and nothing is charged; if the order call fails after
a successful confirm, the authorization expires on its own (capture only happens with the
order).

Ding Dong Dogs is card-only for online orders (Toast reports no pay-at-pickup option), and
only takeout is offered. Delivery, loyalty, gift cards and promo codes are not implemented.

## Wire notes (for anyone extending this)

| | |
| --- | --- |
| Gateway | `https://ws-api.toasttab.com/do-federated-gateway/v1/graphql`. Queries as `GET` with `operationName`, `variables`, `extensions={"persistedQuery":{"version":1,"sha256Hash":H}}`; mutations as `POST` with the same JSON body |
| Persisted operations only | Free-form query text is refused on `POST` ("Forbidden") and introspection is off. `H` is the build-time hash the web bundle attaches to each document (`VAR.__meta__={hash:"..."}`), which is why `dddcli/ops.py` carries a table and `ddd refresh` rebuilds it from the live `public_*.min.js`. Hashes change on Toast deploys |
| Headers | `Toast-GraphQL-Operation`, `Toast-Persistent-Query-Hash`, `Toast-Restaurant-External-ID` (restaurant guid), `apollographql-client-name: sites-web-client`, `apollographql-client-version` (the bundle's VERSION) |
| Session | Mutations need `Toast-Session-ID` from the order page: `<div id="session" data-content="base64 {id, issuedAt, expiresAt}">`. Valid one hour, bound to the client IP. If the API call arrives from a different address family than the page fetch did, Cloudflare answers with its block page, so everything is pinned to IPv4 |
| Cart | `addItemToCartV2` creates the cart when `cartGuid` is null (`createCartInput: {restaurantGuid, orderSource: ONLINE, cartFulfillmentInput: {fulfillmentType: ASAP}, digitalSurface: OO_BASIC}`). `cartV2` (the read) does not echo the cart guid. Selections carry `guid` for `deleteItemFromCartV2` |
| Pickup | `updateFulfillmentAndValidate` with `{fulfillmentType: ASAP\|FUTURE, diningOptionBehavior: TAKE_OUT, fulfillmentDateTime}`; slots come from `diningOptions(futureFulfillmentDaysAhead)`. Toast writes times as `...T19:15:00+0000` in one place and `...T19:30:00.000+00:00` in another |
| Payment | `oo.spiCreatePaymentIntent` (the site also sends a reCAPTCHA Enterprise token; the gateway does without), `oo.spiUpdatePaymentIntent` for tip and tax in cents, `oo.spiGetClientToken` for a JWT, then `POST https://payments.toasttab.com/v1/payment-methods` `{type: CARD, card: {keyId, cardData}, sessionSecret, usage: null, setupFutureUsage: null, billingDetails}` and `POST .../v1/payment-intents/{id}/confirm` `{sessionSecret, paymentMethodData: {scope: SINGLE_USE, type: CARD}, paymentMethodId, email}`, both with `Authorization: Bearer <jwt>`, `Toast-Restaurant-External-ID`, `Toast-HC-Correlation-ID: <intent id>` |
| Card encryption | RSA-OAEP with SHA-1 (MGF1-SHA1, empty label) over `JSON.stringify({cardNumber, zipCode, cvv, expMonth, expYear[, cardholderName]})`, base64. The public key and `keyId` are literals in the hosted checkout iframe (`/assets/checkout.production.*.html`); `dddcli/cardcrypto.py` implements it in the standard library |
| Order | `placeSpiOrder` with `customer {firstName, lastName, email, phone, phoneCountryCode}`, `digitalSurface`, `isCustomDomain`, `tipAmount`, `deliveryCommunicationConsentGiven`, `spiPaymentData {paymentIntentId, paymentMethodId, sessionSecret, saveCard, ccFraudSessionId}`. Returns `PlaceOrderResponse.completedOrder` (check number, promised time, payments) or a `PlaceOrderError` |

## Tests

```sh
.venv/bin/python -m pytest -q
```

Unit tests cover hash extraction from a bundle, session parsing, item and choice
resolution, request shapes, pickup-time parsing and slot snapping, the store, and the
OAEP encoding. Nothing in them touches the network.
