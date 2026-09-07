# Checkout: why `placeSpiOrder` fails

The read/build/price/schedule half of this CLI works. Checkout does not complete: it
authorizes the card (a real pending hold appears) and then the final `placeSpiOrder`
mutation is rejected by Toast with `CRITICAL_ERROR: Sorry, your request failed due to an
unknown error`, so no order is created. This is the record of how far the diagnosis got,
so the next person (or the next live attempt) starts from the facts, not from scratch.

## What is proven (no card was charged to learn any of this)

- **`placeSpiOrder` is the right mutation.** The web app picks the payment path with
  `Hd()`, which is `zd.S(restaurant.i18n.country || "US")`. US resolves to the client-SPI
  path (`serverSpiEnabled = ooServerSpi && !Hd()`), i.e. `placeSpiOrder` with
  `spiPaymentData`. The `semiPaymentIntentId` / `PlacePaidOrder` path is for Adyen markets
  (CA/GB/IE...). Confirmed live: `semiPaymentIntentId` is not even a field on
  `PlacePaidOrderInput`.
- **The request is structurally correct.** It passes GraphQL coercion (only invented
  fields like `sessionId`/`orderSource` are rejected), and it matches the web bundle field
  for field: `{cartGuid, customer{firstName,lastName,email,phone,phoneCountryCode},
  digitalSurface, isCustomDomain, tipAmount, deliveryCommunicationConsentGiven,
  spiPaymentData{paymentIntentId, paymentMethodId, sessionSecret, saveCard,
  ccFraudSessionId}}`. `paymentIntentId` is the confirmed payment's `externalReferenceId`
  (the value the site threads through its confirm callback).
- **The confirm sequence matches the SDK.** `POST /v1/payment-intents/{id}/confirm` with
  `{sessionSecret, paymentMethodData:{scope:SINGLE_USE, type:CARD}, paymentMethodId, email}`
  returns `REQUIRES_CAPTURE`, and the SDK's own readiness check (`rh`) treats
  `REQUIRES_CAPTURE`/`PROCESSING` as done, then places. There is no attach/poll step
  between confirm and place.
- **The crash is specific to SPI capture.** Against the same cart and an unconfirmed
  intent, `placeSpiOrder` throws `CRITICAL_ERROR` (an unhandled server exception) while
  `placeOrder`/`placePaidOrder` returns a graceful `PLACE_ORDER_FAILED`. So the cart,
  customer, pickup time and fulfilment are fine; the fault is in how the SPI capture path
  handles the payment. It also crashed with a genuinely confirmed `REQUIRES_CAPTURE` intent.
- Surface (`OO_BASIC` vs `OO_PRO`) and `ccFraudSessionId` (present, random, or null) make
  no difference.

## The unresolved last mile

An unconfirmed intent always crashes capture (nothing to capture), so no structural tweak
can be validated against one. The only input that exercises the real capture path is a
genuinely confirmed payment, which means a real authorization on a real card. That was
ruled out while debugging. Leading hypotheses to test on the next authorized attempt, most
likely first:

1. **A payment-method/device linkage the hosted iframe establishes and direct API calls do
   not.** The iframe loads Sift and Datadog RUM and may register the payment method with a
   device/session context the capture step dereferences. If so, this may not be drivable
   headlessly at all.
2. **A superfluous `spiUpdatePaymentIntent` before confirm.** This client calls it to set
   tip/tax even when the tip is 0; the site calls it only when the amount changes. Try
   skipping it when the tip is 0.
3. **A missing top-level `surchargeAmount`.** `oo-spi-surcharging-fe` is on for this
   restaurant, so the site adds `input.surchargeAmount` when a surcharge exists. Ding Dong
   Dogs has no surcharge, so this is the weakest lead.

Reproduce the safe probes with a fresh `DDD_CONFIG_DIR`, a Tuesday+ pickup slot (the shop
is closed Monday, so ASAP is off), and an unconfirmed intent. Never point a real confirmed
intent at a throwaway placement.
