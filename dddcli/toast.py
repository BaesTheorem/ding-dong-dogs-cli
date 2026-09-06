"""Transport for the GraphQL gateway behind order.toasttab.com.

What the wire actually requires (all verified live against Ding Dong Dogs, 2026-09-06):

- Cloudflare sits in front of order.toasttab.com and ws-api.toasttab.com and rejects
  non-browser TLS fingerprints outright. curl_cffi's Chrome impersonation passes.
- The gateway (ws-api.toasttab.com/do-federated-gateway/v1/graphql) does not accept
  free-form query text on POST and has introspection off. It executes persisted
  operations only: the web bundle carries a build-time hash per document, queries go
  as GET with that hash, mutations as POST with the same hash. Toast redeploys change
  the hashes, so on PersistedQueryNotFound they are re-read from the live bundle.
- Mutations additionally need a Toast-Session-ID. The restaurant page embeds one
  (<div id="session" data-content="base64 json">), valid for an hour and bound to the
  client IP. The page host has AAAA records and the API host is reached differently,
  so both are pinned to IPv4 to keep the binding intact.
"""

from __future__ import annotations

import base64
import json
import re
import time
from datetime import datetime, timezone

from curl_cffi import requests
from curl_cffi.const import CurlOpt

from dddcli import ops
from dddcli.store import Store

GATEWAY = "https://ws-api.toasttab.com/do-federated-gateway/v1/graphql"
ORDER_HOST = "https://order.toasttab.com"
CLIENT_NAME = "sites-web-client"
DEFAULT_SLUG = "ding-dong-dogs-320-east-51st-street"

_SESSION_RE = re.compile(r'<div id="session" data-content="([^"]*)"')
_BUNDLE_RE = re.compile(r'(https://[^"]+/public_\d+\.min\.js)')
_DOC_RE = re.compile(
    r'([A-Za-z_$][A-Za-z0-9_$]*)=\(0,[A-Za-z_$]+\.[A-Za-z_$]+\)\([A-Za-z_$]+\|\|\([A-Za-z_$]+='
    r'[A-Za-z_$]+\(\["\\n\s*(query|mutation|subscription|fragment) ([A-Za-z0-9_]+)'
)
_META_RE = re.compile(r'([A-Za-z_$][A-Za-z0-9_$]*)\.__meta__=\{hash:"([0-9a-f]+)"\}')
_VERSION_RE = re.compile(r'VERSION:"(\d+)"')


class ToastError(Exception):
    """The gateway, Cloudflare, or the restaurant refused something; message is theirs."""


class GraphQLError(ToastError):
    def __init__(self, op: str, errors: list[dict]):
        self.op = op
        self.errors = errors
        super().__init__(f"{op}: " + "; ".join(e.get("message", "?") for e in errors))


def extract_hashes(bundle: str) -> tuple[dict[str, str], str | None]:
    """Map operation name -> persisted hash from the public web bundle source.

    Each document is defined as VAR=(0,x.y)(t||(t=z(["\\n    query Name(...`))) and later
    tagged VAR.__meta__={hash:"..."}; the pairing is the nearest preceding definition of
    the same variable name, since minified names repeat across modules.
    """
    defs = [(m.start(), m.group(1), m.group(3)) for m in _DOC_RE.finditer(bundle)]
    by_var: dict[str, list[tuple[int, str]]] = {}
    for pos, var, name in defs:
        by_var.setdefault(var, []).append((pos, name))
    mapping: dict[str, str] = {}
    for m in _META_RE.finditer(bundle):
        var, digest = m.group(1), m.group(2)
        earlier = [d for d in by_var.get(var, []) if d[0] < m.start()]
        if not earlier:
            continue
        name = max(earlier, key=lambda d: d[0])[1]
        mapping.setdefault(name, digest)
    ver = _VERSION_RE.search(bundle)
    return mapping, (ver.group(1) if ver else None)


def parse_session(html: str) -> tuple[str, float]:
    """Session id and its expiry (epoch seconds) from the restaurant page."""
    m = _SESSION_RE.search(html)
    if not m:
        raise ToastError("No session token in the restaurant page; Toast may have changed the page.")
    raw = m.group(1).replace("&#x3D;", "=").replace("&#61;", "=")
    data = json.loads(base64.b64decode(raw + "=" * (-len(raw) % 4)))
    expires = datetime.fromisoformat(data["expiresAt"].replace("Z", "+00:00")).timestamp()
    return data["id"], expires


class Transport:
    def __init__(self, store: Store, slug: str = DEFAULT_SLUG, verbose: bool = False):
        self.store = store
        self.slug = slug
        self.verbose = verbose
        self.state = store.restaurant(slug)
        self.hashes = dict(ops.HASHES)
        self.hashes.update(store.hashes)
        self.client_version = (store.load("state.json").get("hashes_meta") or {}).get("version") or ops.CLIENT_VERSION
        self._session: requests.Session | None = None

    # ---- plumbing --------------------------------------------------------------

    @property
    def http(self) -> requests.Session:
        if self._session is None:
            s = requests.Session(impersonate="chrome")
            s.curl.setopt(CurlOpt.IPRESOLVE, 1)  # CURL_IPRESOLVE_V4
            self._session = s
        return self._session

    @property
    def page_url(self) -> str:
        return f"{ORDER_HOST}/online/{self.slug}"

    def _log(self, *parts):
        if self.verbose:
            print("[ddd]", *parts, flush=True)

    def fetch_page(self) -> str:
        r = self.http.get(self.page_url, timeout=60)
        if r.status_code != 200:
            raise ToastError(f"Restaurant page returned HTTP {r.status_code} (Cloudflare challenge?). Try again in a minute.")
        return r.text

    def session_id(self, force: bool = False) -> str:
        sess = self.state.get("session") or {}
        if not force and sess.get("id") and sess.get("expires", 0) - time.time() > 120:
            return sess["id"]
        sid, expires = parse_session(self.fetch_page())
        self.state["session"] = {"id": sid, "expires": expires}
        self.store.save_state()
        self._log("new session id, valid until", datetime.fromtimestamp(expires, tz=timezone.utc).isoformat())
        return sid

    def restaurant_guid(self) -> str:
        guid = self.state.get("guid")
        if guid:
            return guid
        data = self.query("RestaurantIdentifierByShortUrlOO", {"shortUrl": self.slug}, restaurant=False)
        ident = (data.get("oo") or {}).get("restaurantIdentifiersByShortUrl") or {}
        if not ident.get("guid"):
            raise ToastError(f"No Toast restaurant found for slug {self.slug!r}: {ident.get('message') or ident}")
        self.state["guid"] = ident["guid"]
        self.store.save_state()
        return ident["guid"]

    def _headers(self, op: str, restaurant: bool = True) -> dict:
        h = {
            "Accept": "*/*",
            "Origin": ORDER_HOST,
            "Referer": self.page_url,
            "apollographql-client-name": CLIENT_NAME,
            "apollographql-client-version": self.client_version,
            "Toast-GraphQL-Operation": op,
            "Toast-Persistent-Query-Hash": self.hashes[op],
        }
        if restaurant:
            h["Toast-Restaurant-External-ID"] = self.restaurant_guid()
        return h

    def _extensions(self, op: str) -> dict:
        return {"persistedQuery": {"version": 1, "sha256Hash": self.hashes[op]}}

    def _decode(self, op: str, r) -> dict:
        text = r.text
        if not text.startswith("{"):
            if "cloudflare" in text.lower() or r.status_code in (403, 503):
                raise ToastError(f"Cloudflare blocked {op} (HTTP {r.status_code}). Wait a minute and retry.")
            raise ToastError(f"{op}: unexpected HTTP {r.status_code} response: {text[:120]!r}")
        body = json.loads(text)
        errors = body.get("errors") or []
        if errors and not body.get("data"):
            raise GraphQLError(op, errors)
        return body.get("data") or {}

    def _stale(self, err: GraphQLError) -> bool:
        return any(e.get("message") == "PersistedQueryNotFound" for e in err.errors)

    def _forbidden(self, err: GraphQLError) -> bool:
        return any(e.get("message") == "Forbidden" for e in err.errors)

    # ---- public --------------------------------------------------------------

    def query(self, op: str, variables: dict, restaurant: bool = True) -> dict:
        if op not in self.hashes:
            raise ToastError(f"No persisted hash for {op}; run `ddd refresh`.")
        for attempt in (1, 2):
            params = {"operationName": op, "variables": json.dumps(variables), "extensions": json.dumps(self._extensions(op))}
            self._log("GET", op, json.dumps(variables)[:200])
            r = self.http.get(GATEWAY, params=params, headers=self._headers(op, restaurant), timeout=60)
            try:
                return self._decode(op, r)
            except GraphQLError as e:
                if attempt == 1 and self._stale(e):
                    self.refresh_hashes()
                    continue
                raise
        raise AssertionError("unreachable")

    def mutate(self, op: str, variables: dict) -> dict:
        if op not in self.hashes:
            raise ToastError(f"No persisted hash for {op}; run `ddd refresh`.")
        force = False
        for attempt in (1, 2, 3):
            headers = self._headers(op)
            headers["Content-Type"] = "application/json"
            headers["Toast-Session-ID"] = self.session_id(force=force)
            body = {"operationName": op, "variables": variables, "extensions": self._extensions(op)}
            self._log("POST", op, json.dumps(variables)[:300])
            r = self.http.post(GATEWAY, json=body, headers=headers, timeout=60)
            try:
                return self._decode(op, r)
            except GraphQLError as e:
                if attempt < 3 and self._stale(e):
                    self.refresh_hashes()
                    continue
                if attempt < 3 and self._forbidden(e):
                    force = True
                    continue
                raise
        raise AssertionError("unreachable")

    def refresh_hashes(self) -> dict:
        """Re-read every operation hash from the live web bundle and cache them."""
        html = self.fetch_page()
        m = _BUNDLE_RE.search(html)
        if not m:
            raise ToastError("Could not find the public web bundle in the restaurant page.")
        url = m.group(1)
        self._log("downloading", url)
        r = self.http.get(url, timeout=120)
        if r.status_code != 200:
            raise ToastError(f"Bundle download failed: HTTP {r.status_code}")
        mapping, version = extract_hashes(r.text)
        if not mapping:
            raise ToastError("The web bundle changed shape; no operation hashes found.")
        missing = [k for k in ops.HASHES if k not in mapping]
        self.hashes.update(mapping)
        if version:
            self.client_version = version
        self.store.save_hashes(mapping, {"bundle": url.rsplit("/", 1)[-1], "version": version, "fetched": time.time()})
        if missing:
            raise ToastError("Bundle no longer defines: " + ", ".join(missing))
        return mapping
