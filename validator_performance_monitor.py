"""
validator_performance_monitor.py — Validator Performance Monitor.

Fetches real-time validator metrics every N minutes:
  • Vote credits earned per epoch
  • Skip rate (missed slots %)
  • Commission
  • Activated stake
  • Estimated APY

Prints a live terminal dashboard and logs alerts when a tracked validator
degrades significantly.
"""

import time
import json
from dataclasses import dataclass, asdict
from typing import Optional
from datetime import datetime

import requests
from tabulate import tabulate

import config
from utils import (
    get_logger, get_client, lamports_to_sol,
    retry, http_get,
)

log = get_logger("ValidatorMonitor")

# Alert thresholds
SKIP_RATE_ALERT_PCT   = 5.0    # alert if skip rate exceeds this %
APY_DROP_ALERT_PCT    = 0.5    # alert if APY drops more than this
COMMISSION_CAP        = 10     # alert if commission rises above this %


@dataclass
class ValidatorSnapshot:
    vote_pubkey:     str
    name:            str
    commission:      int
    activated_stake: float    # SOL
    epoch_credits:   int
    skip_rate:       float    # %
    apy_estimate:    float    # %
    last_vote:       int
    timestamp:       str = ""

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.utcnow().isoformat()


class ValidatorPerformanceMonitor:
    """Real-time performance tracker for Solana validators."""

    def __init__(self, watch_list: Optional[list[str]] = None):
        """
        Args:
            watch_list: list of vote pubkeys to focus on.
                        If None, monitors the top-50 by stake.
        """
        self.client    = get_client()
        self.watch_list: list[str] = watch_list or []
        self._history: dict[str, list[ValidatorSnapshot]] = {}
        log.info("ValidatorPerformanceMonitor initialised "
                 f"| watching {len(self.watch_list) or 'top-50'} validators")

    # ── RPC helpers ───────────────────────────────────────────────────────────

    @retry(max_attempts=3, delay=2.0)
    def _rpc(self, method: str, params=None) -> any:
        payload = {"jsonrpc": "2.0", "id": 1, "method": method,
                   "params": params or []}
        resp = requests.post(config.SOLANA_RPC_URL, json=payload, timeout=30)
        resp.raise_for_status()
        return resp.json().get("result")

    def _fetch_vote_accounts(self) -> list[dict]:
        result = self._rpc("getVoteAccounts") or {}
        return result.get("current", [])

    def _fetch_epoch_info(self) -> dict:
        return self._rpc("getEpochInfo") or {}

    def _fetch_inflation_rate(self) -> dict:
        return self._rpc("getInflationRate") or {}

    def _fetch_cluster_nodes(self) -> list[dict]:
        return self._rpc("getClusterNodes") or []

    # ── Metadata ──────────────────────────────────────────────────────────────

    @retry(max_attempts=2, delay=3.0)
    def _fetch_name_map(self) -> dict[str, str]:
        """Map vote pubkey → human name from validators.app."""
        try:
            data = http_get(
                "https://www.validators.app/api/v1/validators/mainnet.json",
                params={"limit": 2000},
                timeout=20,
            )
            return {
                v.get("vote_account", ""): v.get("name", "")
                for v in (data or [])
                if v.get("vote_account")
            }
        except Exception:
            return {}

    # ── APY estimation ────────────────────────────────────────────────────────

    def _estimate_apy(self, validator: dict, inflation_rate: float,
                      epoch_info: dict) -> float:
        commission   = validator.get("commission", 100) / 100
        base         = inflation_rate * (1 - commission)
        credits_list = validator.get("epochCredits", [])
        if credits_list:
            last        = credits_list[-1]
            earned      = last[1] - last[2]
            max_credits = epoch_info.get("slotsInEpoch", 432_000)
            perf        = min(earned / max(max_credits, 1), 1.0)
            base       *= perf
        return round(base * 100, 4)

    # ── Skip rate ─────────────────────────────────────────────────────────────

    def _estimate_skip_rate(self, validator: dict, epoch_info: dict) -> float:
        """
        Estimate skip rate from epoch credits delta vs theoretical maximum.
        Skip rate = (slots_expected - slots_voted) / slots_expected * 100
        """
        credits_list = validator.get("epochCredits", [])
        if len(credits_list) < 2:
            return 0.0
        this_epoch  = credits_list[-1]
        prev_epoch  = credits_list[-2]
        earned      = this_epoch[1] - prev_epoch[1]
        max_credits = epoch_info.get("slotsInEpoch", 432_000)
        skip_rate   = max(0.0, (1 - earned / max(max_credits, 1))) * 100
        return round(skip_rate, 2)

    # ── Snapshot collection ───────────────────────────────────────────────────

    def collect_snapshots(self) -> list[ValidatorSnapshot]:
        """Collect a snapshot of all (or watched) validators."""
        vote_accounts    = self._fetch_vote_accounts()
        epoch_info       = self._fetch_epoch_info()
        inflation        = self._fetch_inflation_rate()
        inflation_rate   = float(inflation.get("total", 0.05))
        name_map         = self._fetch_name_map()

        # Filter to watch list if set; else top-50 by stake
        if self.watch_list:
            vote_accounts = [v for v in vote_accounts
                             if v.get("votePubkey") in self.watch_list]
        else:
            vote_accounts = sorted(
                vote_accounts,
                key=lambda x: x.get("activatedStake", 0),
                reverse=True,
            )[:50]

        snapshots = []
        for v in vote_accounts:
            vpk  = v.get("votePubkey", "")
            snap = ValidatorSnapshot(
                vote_pubkey     = vpk,
                name            = name_map.get(vpk, vpk[:16] + "…"),
                commission      = v.get("commission", 100),
                activated_stake = lamports_to_sol(v.get("activatedStake", 0)),
                epoch_credits   = v.get("epochCredits", [[0, 0, 0]])[-1][1],
                skip_rate       = self._estimate_skip_rate(v, epoch_info),
                apy_estimate    = self._estimate_apy(v, inflation_rate, epoch_info),
                last_vote       = v.get("lastVote", 0),
            )
            snapshots.append(snap)
            # Store history for trend analysis
            self._history.setdefault(vpk, []).append(snap)
            if len(self._history[vpk]) > 24:   # keep last 24 cycles (~4 hrs at 10-min intervals)
                self._history[vpk].pop(0)

        return snapshots

    # ── Alerting ──────────────────────────────────────────────────────────────

    def _check_alerts(self, snapshots: list[ValidatorSnapshot]):
        for snap in snapshots:
            # Skip rate alert
            if snap.skip_rate > SKIP_RATE_ALERT_PCT:
                log.warning(
                    f"⚠ HIGH SKIP RATE: {snap.name} skip={snap.skip_rate:.1f}% "
                    f"(threshold {SKIP_RATE_ALERT_PCT}%)"
                )

            # Commission alert
            if snap.commission > COMMISSION_CAP:
                log.warning(
                    f"⚠ HIGH COMMISSION: {snap.name} commission={snap.commission}% "
                    f"(threshold {COMMISSION_CAP}%)"
                )

            # APY degradation alert (compare to previous snapshot)
            history = self._history.get(snap.vote_pubkey, [])
            if len(history) >= 2:
                prev_apy = history[-2].apy_estimate
                apy_drop = prev_apy - snap.apy_estimate
                if apy_drop > APY_DROP_ALERT_PCT:
                    log.warning(
                        f"⚠ APY DROP: {snap.name} "
                        f"{prev_apy:.2f}% → {snap.apy_estimate:.2f}% "
                        f"(dropped {apy_drop:.2f}%)"
                    )

    # ── Dashboard printing ────────────────────────────────────────────────────

    def update_dashboard(self, snapshots: list[ValidatorSnapshot]):
        """Print a formatted dashboard to the terminal."""
        rows = []
        for s in snapshots:
            rows.append([
                s.name[:28],
                f"{s.commission}%",
                f"{s.apy_estimate:.2f}%",
                f"{s.skip_rate:.2f}%",
                f"{s.activated_stake:,.0f}",
                s.last_vote,
            ])

        table = tabulate(
            rows,
            headers=["Validator", "Commission", "Est. APY", "Skip Rate",
                     "Stake (SOL)", "Last Vote"],
            tablefmt="rounded_outline",
            numalign="right",
        )
        separator = "═" * 70
        timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        print(f"\n{separator}")
        print(f"  Validator Performance Dashboard  |  {timestamp}")
        print(f"{separator}")
        print(table)

        # Summary stats
        apys   = [s.apy_estimate for s in snapshots]
        skips  = [s.skip_rate    for s in snapshots]
        if apys:
            print(f"\n  Network avg APY : {sum(apys)/len(apys):.2f}%  "
                  f"| Best APY: {max(apys):.2f}%  "
                  f"| Avg skip rate: {sum(skips)/len(skips):.2f}%")
        print(f"{separator}\n")

    def save_snapshot_json(self, snapshots: list[ValidatorSnapshot],
                           path: str = "validator_snapshots.jsonl"):
        """Append current snapshots to a JSONL file for later analysis."""
        with open(path, "a") as f:
            for s in snapshots:
                f.write(json.dumps(asdict(s)) + "\n")

    # ── Main loop ─────────────────────────────────────────────────────────────

    def run(self):
        log.info("ValidatorPerformanceMonitor running …")
        cycle = 0
        while True:
            try:
                cycle += 1
                log.info(f"Collecting validator snapshots (cycle {cycle}) …")
                snapshots = self.collect_snapshots()
                self._check_alerts(snapshots)
                self.update_dashboard(snapshots)
                self.save_snapshot_json(snapshots)
                log.info(f"Snapshot complete | validators={len(snapshots)}")
            except Exception as exc:
                log.error(f"ValidatorMonitor error: {exc}", exc_info=True)
            time.sleep(config.DASHBOARD_REFRESH_SECONDS * 10)   # 10-min default
