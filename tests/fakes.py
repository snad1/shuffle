"""Fake Shuffle casino for tests (E0-S2).

Speaks the real GraphQL shape the clients parse (shuffle.py and tg/engine.py
`_limbo_parse` / `_dice_parse`, `api_get_balances`). One endpoint:

    POST https://shuffle.com/main-api/graphql/api/graphql
        {"operationName": "LimboPlay" | "DicePlay" | "GetMyProfile", "query", "variables"}
    LimboPlay  variables.data {amount "%.8f", bet "%.2f", currency, windowId, usdAmount}
    DicePlay   variables.data {amount, bet, currency, direction ABOVE|BELOW, usdAmount}
    200 {"data": {"limboPlay"|"dicePlay": {amount, payout, afterBalance (strings),
         shuffleOriginalActions: [{action: {limbo|dice: {resultValue, userValue
         [, userDiceDirection]}}}]}}}
    GetMyProfile -> data.me.account.balances[] with both `amount` (CLI alias)
    and `availableAmount` (TG). Never consumes the script.

Script outcomes: win, loss, insufficient_balance, http_429, timeout,
api_error_balance.
An empty script answers insufficient_balance and sets `exhausted`.
Timeouts surface as requests Timeout (CLI) / httpx.ReadTimeout (TG httpx path).
"""

import json
import uuid
from collections import deque

import httpx
import requests

API_URL = "https://shuffle.com/main-api/graphql/api/graphql"
OUTCOMES = {"win", "loss", "insufficient_balance", "http_429", "timeout",
            "api_error_balance"}
BET_OPS = {"LimboPlay": "limboPlay", "DicePlay": "dicePlay"}


class UnexpectedRequest(BaseException):
    """A request the fake has no route for. BaseException on purpose: the bet
    loops catch Exception and retry forever, which would hang the test."""


class FakeTimeout(Exception):
    """Raised by FakeCasino.handle; adapters map it to the client's timeout."""


class FakeCasino:
    def __init__(self, balance: float = 10.0, currency: str = "USDT", script=()):
        self.balance = float(balance)
        self.currency = currency
        self.script = deque()
        self.calls = []        # (method, url, body) for every request
        self.bets = []         # {"outcome", "amount", "payout", "profit", "balance"}
        self.exhausted = False
        self.queue(*script)

    def queue(self, *outcomes):
        for o in outcomes:
            if o not in OUTCOMES:
                raise ValueError(f"unknown outcome {o!r}")
            self.script.append(o)

    @property
    def bet_posts(self) -> int:
        return sum(1 for _, _, b in self.calls if b.get("operationName") in BET_OPS)

    @property
    def profit(self) -> float:
        return sum(b["profit"] for b in self.bets)

    def handle(self, method: str, url: str, body: dict):
        """Return (status, json_body) or raise FakeTimeout."""
        self.calls.append((method, url, body))
        op = body.get("operationName")
        if method != "POST" or url != API_URL or op not in (*BET_OPS, "GetMyProfile"):
            raise UnexpectedRequest(f"unexpected request {method} {url} {op}")
        if op == "GetMyProfile":
            bal = f"{self.balance:.8f}"
            return 200, {"data": {"me": {"id": "u1", "username": "test", "account": {
                "id": "a1", "balances": [{"currency": self.currency, "amount": bal,
                                          "availableAmount": bal}]}}}}

        data = body["variables"]["data"]
        amount = float(data["amount"])
        outcome = self.script.popleft() if self.script else None
        if outcome is None:
            self.exhausted = True
            outcome = "insufficient_balance"
        if outcome == "timeout":
            raise FakeTimeout()
        if outcome == "http_429":
            return 429, {"message": "Too many requests"}
        if outcome == "api_error_balance":
            # A rejection that mentions "balance" but is not insufficient funds.
            return 200, {"errors": [{"message": "Balance currency not supported"}],
                         "data": None}
        if outcome == "insufficient_balance" or amount > self.balance:
            return 200, {"errors": [{"message": "Insufficient balance",
                                     "extensions": {"code": "INSUFFICIENT_BALANCE"}}],
                         "data": None}

        win = outcome == "win"
        mult = self._payout_mult(op, data)
        payout = round(amount * mult, 8) if win else 0.0
        profit = payout - amount
        self.balance = round(self.balance + profit, 8)
        self.bets.append({"outcome": outcome, "amount": amount, "payout": payout,
                          "profit": profit, "balance": self.balance})
        return 200, {"data": {BET_OPS[op]: self._play(op, data, payout, win)}}

    @staticmethod
    def _payout_mult(op, data) -> float:
        if op == "LimboPlay":
            return float(data["bet"])
        t = float(data["bet"])
        chance = (100 - t) if data["direction"] == "ABOVE" else t
        return round(99.0 / chance, 4)

    def _play(self, op, data, payout, win):
        if op == "LimboPlay":
            uv = float(data["bet"])
            rv = uv + 1.0 if win else 1.0
            action = {"limbo": {"resultRaw": str(rv), "resultValue": f"{rv:.2f}",
                                "userValue": data["bet"], "__typename": "LimboActionModel"}}
        else:
            t = float(data["bet"])
            above = data["direction"] == "ABOVE"
            rv = t + 1 if above == win else t - 1
            action = {"dice": {"userDiceDirection": data["direction"], "userValue": data["bet"],
                               "resultValue": f"{rv:.2f}", "resultRaw": str(rv),
                               "__typename": "DiceActionModel"}}
        return {
            "id": str(uuid.uuid4()),
            "currency": data["currency"],
            "amount": data["amount"],
            "payout": f"{payout:.8f}",
            "afterBalance": f"{self.balance:.8f}",
            "shuffleOriginalActions": [{"id": str(uuid.uuid4()), "action": action}],
        }

    # ── adapters ─────────────────────────────────────────
    def sync_session(self) -> "SyncSession":
        return SyncSession(self)

    def async_client(self) -> httpx.AsyncClient:
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content) if request.content else {}
            try:
                status, payload = self.handle(request.method, str(request.url), body)
            except FakeTimeout:
                raise httpx.ReadTimeout("fake timeout", request=request)
            return httpx.Response(status, json=payload)
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class FakeResponse:
    def __init__(self, status: int, payload):
        self.status_code = status
        self._payload = payload
        self.text = json.dumps(payload)
        self.headers = {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class SyncSession:
    """Stands in for the CLI's module-level `_http` session."""

    def __init__(self, casino: FakeCasino):
        self.casino = casino

    def post(self, url, headers=None, json=None, timeout=None, **_):
        try:
            status, payload = self.casino.handle("POST", url, json or {})
        except FakeTimeout:
            raise requests.exceptions.Timeout("fake timeout")
        return FakeResponse(status, payload)

    def close(self):
        pass
