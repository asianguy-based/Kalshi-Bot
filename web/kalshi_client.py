"""Minimal authenticated Kalshi trade-API v2 client.

The original code never signed a request, so every private endpoint (positions,
orders, balance) would have returned 401 and no order could ever be placed.
Kalshi authenticates with an RSA-PSS signature over
``timestamp_ms + HTTP_METHOD + request_path``, sent as three headers:

    KALSHI-ACCESS-KEY        the key id (a UUID)
    KALSHI-ACCESS-TIMESTAMP  milliseconds since epoch
    KALSHI-ACCESS-SIGNATURE  base64 RSA-PSS/SHA256 signature

The signed path includes the ``/trade-api/v2`` prefix and excludes the query
string.
"""

import base64
import time
import logging

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

logger = logging.getLogger("KalshiBot")

API_BASE = "https://api.elections.kalshi.com"
API_PREFIX = "/trade-api/v2"


class KalshiAuthError(Exception):
    pass


class KalshiClient:
    def __init__(self, key_id=None, private_key_pem=None, timeout=10):
        self.key_id = key_id
        self.timeout = timeout
        self._private_key = None
        if private_key_pem:
            self._private_key = self._load_key(private_key_pem)
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "kalshi-node/1.0"})

    # --- signing ---------------------------------------------------------

    @staticmethod
    def _load_key(pem):
        if isinstance(pem, str):
            pem = pem.encode("utf-8")
        try:
            return serialization.load_pem_private_key(pem, password=None)
        except Exception as exc:
            raise KalshiAuthError(f"Could not parse private key: {exc}") from exc

    def _sign(self, method, path):
        if not (self.key_id and self._private_key):
            raise KalshiAuthError("Missing Kalshi key id or private key.")
        ts = str(int(time.time() * 1000))
        message = (ts + method.upper() + path).encode("utf-8")
        signature = self._private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode(),
            "Content-Type": "application/json",
        }

    # --- transport -------------------------------------------------------

    def _request(self, method, endpoint, params=None, json_body=None, signed=True):
        path = API_PREFIX + endpoint
        url = API_BASE + path
        headers = self._sign(method, path) if signed else {}
        resp = self.session.request(
            method.upper(), url, params=params, json=json_body,
            headers=headers, timeout=self.timeout,
        )
        if resp.status_code >= 400:
            logger.error("Kalshi %s %s -> %s: %s",
                         method, endpoint, resp.status_code, resp.text[:200])
            resp.raise_for_status()
        return resp.json() if resp.content else {}

    # --- public endpoints (no signature required) ------------------------

    def get_markets(self, **params):
        return self._request("GET", "/markets", params=params, signed=False)

    def get_market(self, ticker):
        return self._request("GET", f"/markets/{ticker}", signed=False)

    def get_events(self, **params):
        return self._request("GET", "/events", params=params, signed=False)

    def get_orderbook(self, ticker, depth=10):
        return self._request("GET", f"/markets/{ticker}/orderbook",
                             params={"depth": depth}, signed=False)

    # --- private endpoints ----------------------------------------------

    def get_balance(self):
        return self._request("GET", "/portfolio/balance")

    def get_positions(self, **params):
        return self._request("GET", "/portfolio/positions", params=params)

    def get_orders(self, **params):
        return self._request("GET", "/portfolio/orders", params=params)

    def create_order(self, ticker, side, action, count, price_cents,
                     client_order_id, order_type="limit",
                     time_in_force="fill_or_kill"):
        """Place an order.

        Defaults to a fill-or-kill LIMIT order. This is deliberate: for
        arbitrage, a partial fill on one leg with no fill on the other converts
        a locked-in profit into a naked directional bet. Fill-or-kill either
        gets the whole leg or nothing.
        """
        body = {
            "ticker": ticker,
            "client_order_id": client_order_id,
            "side": side,                 # "yes" | "no"
            "action": action,             # "buy" | "sell"
            "count": int(count),
            "type": order_type,
            "time_in_force": time_in_force,
        }
        if order_type == "limit":
            # Kalshi expects the price on the side being traded, in cents.
            body["yes_price" if side == "yes" else "no_price"] = int(price_cents)
        return self._request("POST", "/portfolio/orders", json_body=body)

    def cancel_order(self, order_id):
        return self._request("DELETE", f"/portfolio/orders/{order_id}")

    def verify_credentials(self):
        """Cheap round trip proving the key works. Returns (ok, detail)."""
        try:
            data = self.get_balance()
            return True, data
        except KalshiAuthError as exc:
            return False, str(exc)
        except requests.HTTPError as exc:
            code = exc.response.status_code if exc.response is not None else "?"
            return False, f"HTTP {code}"
        except Exception as exc:
            return False, str(exc)
