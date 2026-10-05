"""Atomic single-writer budget accounting for explicit acceptance runs.

This is a ledger, not a scheduler. Crashes retain active reservations; unknown
outcomes retain their full bound. Reconciliation requires independent receipts.
No credentials, runtime or network are consulted by this module.
"""
from __future__ import annotations
from contextlib import contextmanager
from decimal import Decimal, ROUND_CEILING
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import time

_SCALE = Decimal(1_000_000_000)


class AcceptanceBudgetError(RuntimeError):
    pass


def _units(value):
    if isinstance(value, bool):
        raise AcceptanceBudgetError("budget must be a finite nonnegative amount")
    try:
        amount = Decimal(str(value))
    except Exception as error:
        raise AcceptanceBudgetError("invalid budget amount") from error
    if not amount.is_finite() or amount < 0:
        raise AcceptanceBudgetError("budget must be a finite nonnegative amount")
    return int((amount * _SCALE).to_integral_value(rounding=ROUND_CEILING))


def _usd(value):
    return float(Decimal(value) / _SCALE)


def provider_evidence_fingerprint(root):
    root = Path(root)
    files = sorted(root.glob("provider-call-*.claim"))
    if (root/"http.jsonl").exists():
        files.append(root/"http.jsonl")
    return hashlib.sha256(json.dumps([(path.name,hashlib.sha256(path.read_bytes()).hexdigest()) for path in files]).encode()).hexdigest()


class AcceptanceBudgetLedger:
    def __init__(self, path, *, limit_usd, initial_known_usd=None, initial_unknown_usd=None):
        limit = _units(limit_usd)
        if not 0 < limit <= _units(100):
            raise AcceptanceBudgetError("round limit must be greater than zero and at most USD 100")
        self.path = Path(path).resolve()
        if not self.path.exists() and (initial_known_usd is None or initial_unknown_usd is None):
            raise AcceptanceBudgetError("a new ledger requires explicit known and unknown opening balances")
        existing_nonempty = self.path.exists() and self.path.stat().st_size > 0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if existing_nonempty and not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='acceptance_budget_meta'").fetchone():
                raise AcceptanceBudgetError("existing file is not an acceptance budget ledger")
            conn.execute("CREATE TABLE IF NOT EXISTS acceptance_budget_meta (id INTEGER PRIMARY KEY CHECK(id=1), limit_units INTEGER NOT NULL, initial_known_units INTEGER NOT NULL, initial_unknown_units INTEGER NOT NULL)")
            conn.execute("CREATE TABLE IF NOT EXISTS acceptance_budget_runs (run_id TEXT PRIMARY KEY, state_root TEXT NOT NULL UNIQUE, state TEXT NOT NULL CHECK(state IN ('active','unknown','settled')), reserved_units INTEGER NOT NULL, known_units INTEGER NOT NULL DEFAULT 0, evidence_json TEXT NOT NULL DEFAULT '{}', updated_at_ms INTEGER NOT NULL)")
            existing = conn.execute("SELECT * FROM acceptance_budget_meta WHERE id=1").fetchone()
            if existing is None:
                if initial_known_usd is None or initial_unknown_usd is None:
                    raise AcceptanceBudgetError("a new ledger requires explicit known and unknown opening balances")
                known, unknown = _units(initial_known_usd), _units(initial_unknown_usd)
                if known + unknown > limit:
                    raise AcceptanceBudgetError("opening balances exceed the explicit round limit")
                conn.execute("INSERT INTO acceptance_budget_meta VALUES(1,?,?,?)", (limit,known,unknown))
            elif ((initial_known_usd is not None and _units(initial_known_usd) != existing["initial_known_units"])
                  or (initial_unknown_usd is not None and _units(initial_unknown_usd) != existing["initial_unknown_units"])):
                raise AcceptanceBudgetError("existing opening balances are immutable; do not reset prior spend")
            if conn.execute("SELECT limit_units FROM acceptance_budget_meta WHERE id=1").fetchone()[0] != limit:
                raise AcceptanceBudgetError("existing ledger limit is immutable; use its original round limit")

    @contextmanager
    def _connection(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _totals(conn):
        opening = conn.execute("SELECT * FROM acceptance_budget_meta WHERE id=1").fetchone()
        limit = int(opening["limit_units"])
        rows = conn.execute("SELECT state,reserved_units,known_units FROM acceptance_budget_runs").fetchall()
        known = int(opening["initial_known_units"]) + sum(int(row["known_units"]) for row in rows if row["state"] == "settled")
        unknown = int(opening["initial_unknown_units"]) + sum(int(row["reserved_units"]) for row in rows if row["state"] == "unknown")
        reserved = sum(int(row["reserved_units"]) for row in rows if row["state"] == "active")
        return limit, known, unknown, reserved

    def snapshot(self):
        with self._connection() as conn:
            limit, known, unknown, reserved = self._totals(conn)
            active = [row[0] for row in conn.execute("SELECT run_id FROM acceptance_budget_runs WHERE state='active' ORDER BY run_id")]
        return {"limitUsd": _usd(limit), "knownUsd": _usd(known), "unknownReservedUsd": _usd(unknown),
                "activeReservedUsd": _usd(reserved), "remainingUsd": _usd(max(0, limit-known-unknown-reserved)), "activeRunIds": active,
                "openingBalanceAuthority":"explicit caller values; no home-directory discovery or implicit zero reset"}

    def admit(self, run_id, *, state_root, reserve_usd):
        reserve = _units(reserve_usd)
        if not run_id or reserve <= 0:
            raise AcceptanceBudgetError("run identity and positive worst-case reservation are required")
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            limit, known, unknown, active = self._totals(conn)
            if conn.execute("SELECT 1 FROM acceptance_budget_runs WHERE state='active' LIMIT 1").fetchone():
                raise AcceptanceBudgetError("another active run owns the ledger; reconcile it before admitting work")
            if known + unknown + active + reserve > limit:
                raise AcceptanceBudgetError("known plus unknown plus reserved cost would exceed the round limit")
            try:
                conn.execute("INSERT INTO acceptance_budget_runs(run_id,state_root,state,reserved_units,updated_at_ms) VALUES(?,?,'active',?,?)",
                             (str(run_id), str(Path(state_root).resolve()), reserve, int(time.time()*1000)))
            except sqlite3.IntegrityError as error:
                raise AcceptanceBudgetError("run/state identity was already admitted; no automatic replay") from error
            return {"runId": run_id, "reservedUsd": _usd(reserve), "priorKnownUsd": _usd(known+unknown), "limitUsd": _usd(limit)}

    def mark_unknown(self, run_id, *, reason):
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT state FROM acceptance_budget_runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                raise AcceptanceBudgetError("unknown run identity")
            if row["state"] == "settled":
                return
            conn.execute("UPDATE acceptance_budget_runs SET state='unknown',evidence_json=?,updated_at_ms=? WHERE run_id=?",
                         (json.dumps({"reason": str(reason)[:80]}), int(time.time()*1000), run_id))

    def settle(self, run_id, *, actual_usd, proof):
        actual = _units(actual_usd)
        claims = proof.get("httpClaims")
        accounted = proof.get("accountedProviderCalls")
        if (proof.get("guardLoaded") is not True or proof.get("exactUsage") is not True
            or proof.get("hostClosed") is not True or proof.get("noPending") is not True
            or proof.get("guardProcessesExited") is not True
            or type(claims) is not int or claims < 0 or claims != accounted
            or proof.get("successfulHttpResponses") != claims):
            raise AcceptanceBudgetError("guard, HTTP claims and exact usage do not establish settlement")
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM acceptance_budget_runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                raise AcceptanceBudgetError("unknown run identity")
            if row["state"] == "settled":
                if int(row["known_units"]) != actual:
                    raise AcceptanceBudgetError("settled cost cannot be changed")
                return
            if actual > int(row["reserved_units"]):
                raise AcceptanceBudgetError("actual cost exceeds reserved bound; keep reservation and investigate")
            conn.execute("UPDATE acceptance_budget_runs SET state='settled',known_units=?,evidence_json=?,updated_at_ms=? WHERE run_id=?",
                         (actual, json.dumps(proof, sort_keys=True), int(time.time()*1000), run_id))


def catalog_reservation(catalog, calls):
    if type(calls) is not int or not 1 <= calls <= 12:
        raise AcceptanceBudgetError("provider-call bound must be 1..12")
    if catalog.get("id") != "gpt-6.1-sol":
        raise AcceptanceBudgetError("the verified guard supports only gpt-6.1-sol")
    context, output = catalog.get("contextWindow"), catalog.get("maxTokens")
    if type(context) is not int or type(output) is not int or context <= 0 or output <= 0:
        raise AcceptanceBudgetError("full model context/output bounds are required")
    cost = catalog.get("cost") or {}
    def rate(value):
        if type(value) not in (int,float) or not math.isfinite(value) or value < 0:
            raise AcceptanceBudgetError("complete finite catalog pricing is required")
        return Decimal(str(value))
    input_rate, output_rate = rate(cost.get("input"))*4, rate(cost.get("output"))*4
    for tier in [cost, *(cost.get("tiers") or [])]:
        input_rate = max(input_rate, *(rate(tier.get(key, 0)) for key in ("input","cacheRead","cacheWrite")))
        output_rate = max(output_rate, rate(tier.get("output", 0)))
    each = (Decimal(context)*input_rate + Decimal(output)*output_rate)/Decimal(1_000_000)
    return {"perCallReservedUsd": _usd(_units(each)), "reservedUsd": _usd(_units(each*calls)),
            "maxProviderRequests": calls, "maxRequestBytes": min(context,1_000_000),
            "catalog": catalog, "costBasis": "full catalog limits; max of 4x base input/output and all catalog tiers"}


class AcceptanceRunBudget:
    """Verified guard interface consumed by the existing acceptance adapters."""
    def __init__(self, *, root, reservation, max_tools=32):
        self.root = Path(root).resolve()
        self.reservation = dict(reservation)
        self.max_provider_calls = int(reservation["maxProviderRequests"])
        self.case_limit_usd = float(reservation["reservedUsd"])
        self.max_tool_calls = max_tools
        self.rows = []
        self.service = None
        self.tool_calls = 0
        self.started = time.monotonic()
        self.timeout = 180
        self.quiescence = None

    def require_loaded(self):
        path = self.root/"guard-loaded.jsonl"
        if not path.is_file() or not any(json.loads(line).get("pid") for line in path.read_text().splitlines() if line):
            raise AcceptanceBudgetError("provider guard did not load in the prepared Host")

    @contextmanager
    def guard(self, **limits):
        self.require_loaded()
        if limits["max_provider_calls"] > self.max_provider_calls or limits["case_limit_usd"] != self.case_limit_usd:
            raise AcceptanceBudgetError("adapter cannot expand the admitted reservation")
        self.max_tool_calls = min(self.max_tool_calls, limits["max_tool_calls"])
        self.timeout = min(300, limits["timeout_seconds"])
        self.started = time.monotonic()
        yield self

    def check_tool_admission(self):
        self.tool_calls += 1
        if self.tool_calls > self.max_tool_calls or time.monotonic()-self.started > self.timeout:
            raise AcceptanceBudgetError("bounded tool/time admission exceeded")

    def account_exact_turn(self, sid, tid, usage):
        if self.service is None:
            raise AcceptanceBudgetError("budget has no bound isolated Service")
        path = Path(self.service.sessions.get(sid)["sessionFile"]).resolve()
        if not path.is_relative_to(self.root/"service"):
            raise AcceptanceBudgetError("transcript is outside the isolated Service state")
        row = self._normalize_turn(sid, tid, path, usage)
        if any(item["sessionId"] == sid and item["turnId"] == tid for item in self.rows):
            raise AcceptanceBudgetError("exact turn was already accounted")
        self.rows.append(row)

    def _normalize_turn(self, sid, tid, path, usage):
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != usage["transcriptSha256"]:
            raise AcceptanceBudgetError("transcript changed during usage accounting")
        active = False
        paid, blocked = [], []
        for line in raw.splitlines():
            entry = json.loads(line)
            if entry.get("customType") == "rag-ime.pi-turn-binding":
                active = entry.get("data",{}).get("turnId") == tid
            message = entry.get("message",{})
            if not active or entry.get("type") != "message" or message.get("role") != "assistant":
                continue
            values = message.get("usage") or {}
            amount = (values.get("cost") or {}).get("total")
            if (message.get("stopReason") == "error" and message.get("errorMessage") in
                {"PAW_PROVIDER_CALL_LIMIT","PAW_GUARD_ZERO_REQUEST_PROOF"} and values.get("totalTokens") == 0 and amount == 0):
                if not (self.root/"denied.jsonl").is_file():
                    raise AcceptanceBudgetError("zero-cost local error lacks pre-network denial evidence")
                blocked.append(entry.get("id"))
            else:
                if type(amount) not in (int,float) or not math.isfinite(amount) or amount <= 0:
                    raise AcceptanceBudgetError("actual provider cost is missing or uncertain")
                paid.append(amount)
        return {"sessionId":sid,"turnId":tid,"sessionFile":str(path),"providerCalls":len(paid),"estimatedUsd":sum(paid),
                "transcriptSha256":usage["transcriptSha256"],"localDeniedEntryIds":blocked}

    def account_compaction(self, *_args):
        raise AcceptanceBudgetError("compaction is not included in this packaged case set")

    def finish(self):
        """Return a provisional cost view; it cannot settle an active Host."""
        self.require_loaded()
        claims = len(list(self.root.glob("provider-call-*.claim")))
        calls = sum(row["providerCalls"] for row in self.rows)
        http_path = self.root/"http.jsonl"
        responses = [json.loads(line) for line in http_path.read_text().splitlines() if line] if http_path.exists() else []
        if claims != calls or calls > self.max_provider_calls or len(responses) != claims or any(row.get("status") != 200 for row in responses):
            raise AcceptanceBudgetError("HTTP attempts/responses and exact-turn usage differ; retain reservation")
        actual = sum(row["estimatedUsd"] for row in self.rows)
        if not math.isfinite(actual) or actual > self.case_limit_usd:
            raise AcceptanceBudgetError("actual usage exceeds its reservation")
        closed = bool(self.quiescence and all(self.quiescence.get(key) is True for key in ("hostClosed","noPending","guardProcessesExited")))
        proof = {"guardLoaded":True,"exactUsage":closed,"hostClosed":closed,"noPending":closed,"guardProcessesExited":closed,
                 "httpClaims":claims,"accountedProviderCalls":calls,
                 "successfulHttpResponses":len(responses),"transcriptHashes":[row["transcriptSha256"] for row in self.rows]}
        return {"estimatedUsd":actual,"providerCalls":calls,"guardClaims":claims,"usageUnknown":not closed,"settlementReady":closed,
                "reservedUsd":self.case_limit_usd,"reservationReleasedUsd":self.case_limit_usd-actual if closed else 0,"proof":proof}

    def evidence_fingerprint(self):
        return provider_evidence_fingerprint(self.root)

    def finalize(self, quiescence):
        if not all(quiescence.get(key) is True for key in ("hostClosed","noPending","guardProcessesExited")):
            raise AcceptanceBudgetError("runtime shutdown did not prove quiescence; retain reservation")
        if quiescence.get("evidenceFingerprint") != self.evidence_fingerprint():
            raise AcceptanceBudgetError("provider evidence changed after shutdown; retain reservation")
        from .micro import turn_usage
        # Re-read final transcript bytes after the Host has exited. An adapter's
        # earlier successful settlement is not proof that no late work occurred.
        self.rows = [self._normalize_turn(row["sessionId"],row["turnId"],Path(row["sessionFile"]),
                                         turn_usage(row["sessionFile"],row["turnId"])) for row in self.rows]
        self.quiescence = dict(quiescence)
        return self.finish()
