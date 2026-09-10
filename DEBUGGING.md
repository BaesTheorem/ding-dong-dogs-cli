# Checkout: the `placeSpiOrder` failure, and what it was

Earlier versions of `ddd checkout` authorized the card and then had `placeSpiOrder` rejected
with `CRITICAL_ERROR: Sorry, your request failed due to an unknown error`, leaving a pending
hold and no order. The cause was found by reading the order page's flag bootstrap together with the checkout code in
the web bundle (`public_1788969381.min.js`, client version 3822). This file keeps the
finding and the reasoning so the next person can check it rather than redo it.

## What the web app actually does

Toast has two card flows, and the order page says which one a restaurant runs:
`window.__FLAGS_STATE__["oo-server-spi"]`, true for Ding Dong Dogs. The country in the
page's Apollo state matters too: non-US restaurants go through Adyen and `PlaceCcOrder`.

The checkout callback (`ke` in the bundle; `p` is `serverSpiEnabled`, `R` the intent,
`s` the gift-card flow, `v` the payments SDK) reads, de-minified:

```js
else if (r && (i = await r()), i && p && R) {
  await n(R.id, ..., {paymentMethodId, surchargeAmount, sessionSecret: R.sessionSecret})
} else if (i) {
  await Ce(...)                                   // spiUpdatePaymentIntent
}
i ? (p && !s || (await v.confirmPayment(async e => {
      await n(e.content.payment.externalReferenceId, ..., d)   // d carries no sessionSecret
    }, Oe(a)))) : a()
```

and the placement (`placeOrder` in `gP`; `i` is `serverSpiEnabled`):

```js
if (i) { input.spiPaymentData = {paymentIntentId: t, paymentMethodId, sessionSecret, saveCard, ccFraudSessionId}; placeSpiOrder }
else   { input.paymentId = t; placePaidOrder }
```

So the two flows are:

| | server SPI (`oo-server-spi` on) | client SPI (off) |
| --- | --- | --- |
| intent update | none; the tip rides on the order | `spiUpdatePaymentIntent`, always |
| client-side confirm | **none** | `POST /v1/payment-intents/{id}/confirm` (the authorization) |
| placement | `placeSpiOrder`, `paymentIntentId` = the intent id | `placePaidOrder`, `paymentId` = the confirmed payment's `externalReferenceId` |
| who authorizes and captures | Toast, inside `placeSpiOrder` | the client authorizes, Toast captures |

The payments SDK's `createPaymentMethod` only forwards to the hosted iframe, which POSTs
`{type: CARD, card: {keyId, cardData}, sessionSecret, usage, setupFutureUsage, billingDetails}`
to `/v1/payment-methods` and nothing else; there is no attach step.

## What this tool did

Create, update, tokenize, **confirm**, then `placeSpiOrder` with the confirmed payment's
`externalReferenceId`. That is the left column's mutation fed the right column's state.
`placeSpiOrder` confirms the intent server-side; asked to confirm one that `/confirm` had
already moved to `REQUIRES_CAPTURE`, it throws, and that surfaces as `CRITICAL_ERROR`.

This also explains every observation that misled the earlier sessions:

- The unconfirmed-intent probes crashed the same way at four restaurants. They carried no
  real payment method, so the server-side confirm inside `placeSpiOrder` threw on those
  too. It was never a capture failure; nothing was ever captured in either case.
- `placePaidOrder` on the same cart failed gracefully because it looks a payment up by id
  and simply does not find a made-up one.
- Retrying the place never helped: the intent stayed confirmed, so every retry hit the
  same double confirm.
- The pre-confirm hypotheses (surcharge field, Sift or device linkage, the redundant
  update) were all downstream of a flow that never matched the page.

## What changed

`Transport` reads the flag bootstrap and the country whenever it fetches the page (it
already fetched it for the session id). `checkout.flow()` picks the flow and
`ddd checkout --dry-run` prints it. The server flow never calls `/confirm`; the client flow
always updates, confirms, and places with `placePaidOrder`, sending the payment method id
and the update's `surchargeAmount` when `oo-spi-surcharging-fe` is on. One session id is
generated per checkout and sent both as the intent's `sessionId` and the order's
`ccFraudSessionId`, as the page does with its Sift session.

## Still unverified

The fix is derived from the page and the bundle, not yet from a placed order. The first
real `ddd checkout` under the new flow is the test: it should return a check number and a
Toast receipt email, and the card should show a capture rather than a hold. If it fails
instead, where Toast stopped decides the cost: a refusal before its own confirm leaves
nothing, a crash after it leaves one hold that drops like the earlier ones did.

A related Toast defect, not this one: a quantity-N line carrying a priced modifier makes
the cart and the created intent disagree by a cent (two quantity-1 lines match). The cart taxes the line whole, the intent looks
to tax per unit and round each. In the server flow the intent amount is Toast's to
reconcile.
