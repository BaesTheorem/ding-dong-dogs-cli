"""Card-data encryption matching Toast's hosted checkout iframe.

The iframe (payments.toasttab.com/assets/checkout.production.*.html) encrypts
JSON.stringify({cardNumber, zipCode, cvv, expMonth, expYear[, cardholderName]}) with
RSA-OAEP over SHA-1 using a public key baked into its own source, then posts
{keyId, cardData: base64} to /v1/payment-methods. This is that scheme in the standard
library, so the CLI carries no crypto dependency. PEM parsing is limited to the
SubjectPublicKeyInfo shape those keys use.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os

# Key the iframe selects when it is served from payments.toasttab.com (its only production host).
KEY_ID = "RSA-OAEP-SHA1::bacb4887-8314-4c23-98fc-d5ef7bc99e94_oo-legacy-browsers"
PUBLIC_PEM_B64 = (
    "LS0tLS1CRUdJTiBQVUJMSUMgS0VZLS0tLS0KTUlJQklqQU5CZ2txaGtpRzl3MEJBUUVGQUFPQ0FROEFNSUlCQ2dLQ0FRRUFrcTUwZFhkei9mUU93WVVzd1pSWApnaDVWZ2hzMXRjSlpDdjJnb05zQzVCTzNkWEMvRWllamNBcGFFL2ZLVk1lc3B6RE5TTHNRSFhyV0hlZ3BFMnBpCnllQndNenBUYkhyQ0czdjZNbkc2TnUrOWRCWXhQUzRUOWNOTXNmMFYvaUNQaWlhVFhpYjZyOElmT2pWWlRUYXMKMm5LR1pFdGlEZllKSVMzYkovZjMwU01QNGRBRklMdnpYR0FMZXFsZUIvcHNROEFGYy9xVVpmN1ZacFJub0d0Swpvbzh3RDI1M3hUbURkYWlLcXIwOWJjSlp4aFRXc0NCOWFZb3VxZGJsNnk5LzVMdStNdHlCa2o3TEkwNWc1UXFEClNmVmF4MHNwL0ZlN1JZNVNZeHdsUjRHbE1vbnd0YnZGVE1Lc3I2ZFoxdUx1TmpIQS82ZG0rMTEyVXFuL0F4cksKR3dJREFRQUIKLS0tLS1FTkQgUFVCTElDIEtFWS0tLS0tCg=="
)


def _tlv(der: bytes, i: int) -> tuple[int, int, int]:
    """(tag, content start, content end) of the DER element at offset i."""
    tag = der[i]
    length = der[i + 1]
    i += 2
    if length & 0x80:
        n = length & 0x7F
        length = int.from_bytes(der[i : i + n], "big")
        i += n
    return tag, i, i + length


def parse_public_key(pem: str) -> tuple[int, int]:
    """(modulus, exponent) from a PEM SubjectPublicKeyInfo (PKCS#1 RSA inside)."""
    body = "".join(line for line in pem.strip().splitlines() if not line.startswith("-----"))
    der = base64.b64decode(body)
    _, start, _ = _tlv(der, 0)  # outer SEQUENCE
    _, _, alg_end = _tlv(der, start)  # AlgorithmIdentifier SEQUENCE
    tag, bs_start, _ = _tlv(der, alg_end)  # BIT STRING
    if tag != 0x03:
        raise ValueError("unexpected key structure")
    inner = bs_start + 1  # skip the unused-bits byte
    _, seq_start, _ = _tlv(der, inner)  # RSAPublicKey SEQUENCE
    tag, n_start, n_end = _tlv(der, seq_start)
    tag2, e_start, e_end = _tlv(der, n_end)
    if tag != 0x02 or tag2 != 0x02:
        raise ValueError("unexpected key structure")
    return int.from_bytes(der[n_start:n_end], "big"), int.from_bytes(der[e_start:e_end], "big")


def _mgf1(seed: bytes, length: int) -> bytes:
    out = b""
    counter = 0
    while len(out) < length:
        out += hashlib.sha1(seed + counter.to_bytes(4, "big")).digest()
        counter += 1
    return out[:length]


def oaep_encrypt(message: bytes, n: int, e: int, rand: bytes | None = None) -> bytes:
    """RSAES-OAEP with SHA-1 and MGF1-SHA1, empty label (RFC 8017 section 7.1.1)."""
    k = (n.bit_length() + 7) // 8
    h_len = 20
    if len(message) > k - 2 * h_len - 2:
        raise ValueError("message too long for this key")
    l_hash = hashlib.sha1(b"").digest()
    ps = b"\x00" * (k - len(message) - 2 * h_len - 2)
    db = l_hash + ps + b"\x01" + message
    seed = rand if rand is not None else os.urandom(h_len)
    masked_db = bytes(a ^ b for a, b in zip(db, _mgf1(seed, k - h_len - 1), strict=True))
    masked_seed = bytes(a ^ b for a, b in zip(seed, _mgf1(masked_db, h_len), strict=True))
    em = b"\x00" + masked_seed + masked_db
    return pow(int.from_bytes(em, "big"), e, n).to_bytes(k, "big")


def encrypt_card(number: str, exp_month: str, exp_year: str, cvv: str, zip_code: str, name: str | None = None) -> tuple[str, str]:
    """(keyId, base64 ciphertext) the way the iframe builds them; field order matters only for
    byte-for-byte parity with the browser and is kept anyway."""
    payload = {"cardNumber": number, "zipCode": zip_code, "cvv": cvv, "expMonth": exp_month, "expYear": exp_year}
    if name and name.strip():
        payload["cardholderName"] = name.strip()
    n, e = parse_public_key(base64.b64decode(PUBLIC_PEM_B64).decode())
    blob = json.dumps(payload, separators=(",", ":")).encode()
    return KEY_ID, base64.b64encode(oaep_encrypt(blob, n, e)).decode()
