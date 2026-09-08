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
- ~~**The crash is specific to SPI capture.**~~ **Retracted 2026-09-08.** The original
  reading was that `placeSpiOrder` throwing `CRITICAL_ERROR` against an unconfirmed intent,
  where `placePaidOrder` fails gracefully, pointed at this restaurant's SPI capture path.
  It does not point anywhere. The same unconfirmed-intent call returns the identical
  `CRITICAL_ERROR` at King G, Scott's Kitchen and La Bodega KC as well, so it is just what
  Toast does when asked to capture a payment that was never confirmed. It is an expected
  crash on a nonsense request and carries no information about the real failure. Every
  probe built on it should be treated as uninformative rather than as evidence. The real
  failure, with a genuinely confirmed `REQUIRES_CAPTURE` intent, has still only been seen twice, both live.
- Surface (`OO_BASIC` vs `OO_PRO`) and `ccFraudSessionId` (present, random, or null) make
  no difference.


## The cart and the intent can disagree by a cent

Found while checking why the tipless update guard still fired. Toast prices a
line of quantity N by taxing the whole line, but `spiCreatePaymentIntent` appears to tax
per unit and round each one, so the two disagree whenever the per-unit tax lands on a half
cent:

Measured on a handful of probe carts: quantity-1 lines match exactly, and a quantity-2 line
carrying a priced modifier lands a cent apart.

Two per-unit taxes rounded separately can land a cent below the same amount taxed as one line;
the same food as two quantity-1 lines makes the cart and the intent match exactly.

This is a real defect, but do not assume it is *the* defect. Both failed checkouts happened to
use this shape, which is suggestive and nothing more.
It is also actively weakened by the fact that `spiUpdatePaymentIntent` reconciles the
amount before the card is charged: the second confirm returned the
correct total, and the place still crashed.

## Retrying the place is free, so do that first

The authorization is created by `/confirm`. Placing against an already confirmed intent
does not create another one, so a single hold pays for as many `placeSpiOrder` attempts as
you like. `place_order_with_retry` now waits out `CRITICAL_ERROR` five times at four second
intervals and raises any graceful refusal immediately. If the capture failure is a
propagation race between confirm and place, this fixes it outright; if the next attempt
crashes all five times, the race hypothesis is dead and the cost was one hold, not five.
Checkout now also logs the created intent amount against the cart total, which the
second run could not be read back for.

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
2. ~~**A superfluous `spiUpdatePaymentIntent` before confirm.**~~ Done, 2026-09-08.
   `spiCreatePaymentIntent` already returns the tax-inclusive cart total (a $4.00 cart
   with $0.48 tax creates an intent with `amount: 448`, `captureMethod: MANUAL`), so a
   tipless order has nothing left to set and the site makes no update call. Skipping it
   therefore cannot drop the tax, which was the reason to be careful about this one.
   `update_intent_if_needed` now sends the update only when it would change the amount.
   Untested against a real authorization.
3. **A missing top-level `surchargeAmount`.** `oo-spi-surcharging-fe` is on for this
   restaurant, so the site adds `input.surchargeAmount` when a surcharge exists. Ding Dong
   Dogs has no surcharge, so this is the weakest lead.

A redeploy does not clear this. After Toast shipped client version 3813 (`ddd refresh`
re-read 100 hashes on 2026-09-08), the unconfirmed-intent probe still returns
`CRITICAL_ERROR`. That is only weak evidence, though: an unconfirmed intent crashes
capture whether or not the server bug is fixed, so this probe cannot tell the two apart.
Do not read it as proof the bug survives.

Reproduce the safe probes with a fresh `DDD_CONFIG_DIR`, a Tuesday+ pickup slot (the shop
is closed Monday, so ASAP is off), and an unconfirmed intent. Never point a real confirmed
intent at a throwaway placement.
