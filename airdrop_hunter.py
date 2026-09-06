"""
airdrop_hunter.py — Airdrop Hunter.

Sources:
  • DefiLlama airdrop list (https://defillama.com/airdrops)
  • Solana ecosystem announcements via GitHub watchers
  • On-chain interaction tracker: ensures your wallet has interacted with
    protocols that historically reward active users (e.g. DEX swaps,
    lending deposits, governance votes).

This module does NOT auto-farm airdrops by spamming contracts.
It tracks LEGITIMATE participation by monitoring your wallet's real usage
and flagging protocols where you are NOT yet eligible based on past patterns.
"""

import time
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import requests

import config
from utils import (
    get_logger, get_client, load_keypair,
    lamports_to_sol, http_get, retry,
)

log = get_logger("AirdropHunter")

DEFILLAMA_AIRDROPS_URL = "https://api.llama.fi/protocols"

# Known Solana protocols worth interacting with for airdrop eligibility.
# Each entry: (protocol_name, program_id, interaction_description)
SOLANA_PROTOCOLS_TO_TRACK = [
    ("Jupiter",      "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4", "DEX swap"),
    ("Marinade",     "MarBmsSgKXdrN1egZf5sqe1TMai9K1rChYNDJgjq7aD",  "mSOL staking"),
    ("Jito",         "jtojtomepa8b1tDPp6cW7KE1suDmJ5vzqhEfQEoUEXT8", "JTO staking"),
    ("Drift",        "dRiftyHA39MWEi3m9aunc5MzRF1JYuBsbn6VPcn33UH",  "perp trading"),
    ("Marginfi",     "MFv2hWf31Z9kbCa1snEPdcgp168vCzXx2hUbFKVGCBB",  "lending"),
    ("Kamino",       "KLend2g3cP87fffoy8q1mQqGKjrxjC8boSyAYavgmjD",  "lending/yield"),
    ("Meteora",      "Eo7WjKq67rjJQSZxS6z3YkapzY3eMj6Xy8X5EkAW51Vb", "DLMM LP"),
    ("Pyth",         "pythWSnswVUd12oZpeFP8e9CVaEqJg25g1Vtc2biRsT",   "oracle staking"),
    ("Squads",       "SQDS4ep65T869zMMBKyuUq6aD6EgTu8psMjkvj52pCf",  "multisig"),
    ("Tensor",       "TSWAPaqyCSx2KABk68Shruf4rp7CxcAi9UTztBioRcT",  "NFT trading"),
]


@dataclass
class AirdropOpportunity:
    protocol:        str
    description:     str
    estimated_value: str    # e.g. "$100-$1000"
    deadline:        str    # ISO date or "ongoing"
    eligibility:     str    # "eligible" | "unknown" | "interacted"
    action_needed:   str    # what to do to qualify
    source_url:      str = ""


@dataclass
class WalletActivity:
    program_id:  str
    tx_count:    int
    first_tx:    Optional[str] = None
    last_tx:     Optional[str] = None


class AirdropHunter:
    """
    Monitors airdrop opportunities and checks wallet eligibility
    based on on-chain interaction history.
    """

    def __init__(self):
        self.client  = get_client()
        self.keypair = load_keypair()
        self.pubkey  = str(self.keypair.pubkey())
        self._known_opportunities: list[AirdropOpportunity] = []
        log.info(f"AirdropHunter initialised | wallet={self.pubkey}")

    # ── On-chain activity checks ───────────────────────────────────────────────

    @retry(max_attempts=3, delay=2.0)
    def _get_tx_count_for_program(self, program_id: str, limit: int = 100) -> int:
        """
        Counts recent transactions from this wallet that mention a given program.
        Uses getSignaturesForAddress with a mention filter.
        """
        import requests as _r
        payload = {
            "jsonrpc": "2.0", "id": 1,
            "method": "getSignaturesForAddress",
            "params": [self.pubkey, {"limit": limit, "commitment": "confirmed"}],
        }
        resp = _r.post(config.SOLANA_RPC_URL, json=payload, timeout=15)
        resp.raise_for_status()
        sigs = resp.json().get("result", []) or []

        # Count transactions involving this program (check via getTransaction)
        # For efficiency we use a heuristic: count sigs in the same slot range
        # and check a sample for program involvement.
        count = 0
        sample = sigs[:20]  # check last 20 txs for program involvement
        for sig_info in sample:
            try:
                tx_payload = {
                    "jsonrpc": "2.0", "id": 1,
                    "method": "getTransaction",
                    "params": [sig_info["signature"],
                               {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
                }
                tx_resp = _r.post(config.SOLANA_RPC_URL, json=tx_payload, timeout=10)
                tx_data = tx_resp.json().get("result") or {}
                account_keys = (
                    (tx_data.get("transaction") or {})
                    .get("message", {})
                    .get("accountKeys", [])
                )
                for key in account_keys:
                    k = key if isinstance(key, str) else key.get("pubkey", "")
                    if k == program_id:
                        count += 1
                        break
            except Exception:
                pass
        return count

    def _check_wallet_interactions(self) -> dict[str, WalletActivity]:
        """Return interaction counts for all tracked protocols."""
        activity: dict[str, WalletActivity] = {}
        for name, program_id, _ in SOLANA_PROTOCOLS_TO_TRACK:
            count = self._get_tx_count_for_program(program_id)
            activity[name] = WalletActivity(program_id=program_id, tx_count=count)
            log.debug(f"  {name}: {count} interactions found")
        return activity

    # ── DefiLlama airdrop list ─────────────────────────────────────────────────

    @retry(max_attempts=3, delay=2.0)
    def _fetch_defillama_protocols(self) -> list[dict]:
        """Fetch all DeFi protocols from DefiLlama to find Solana ones."""
        data = http_get(DEFILLAMA_AIRDROPS_URL, timeout=20)
        return [p for p in (data or []) if "solana" in (p.get("chains") or [])]

    def _build_opportunity_list(self, activity: dict[str, WalletActivity]) -> list[AirdropOpportunity]:
        """
        Cross-reference tracked protocols with wallet activity to produce
        a prioritised list of airdrop opportunities.
        """
        opportunities: list[AirdropOpportunity] = []

        # Hardcoded high-value Solana airdrop candidates (updated regularly)
        candidates = [
            {
                "protocol":        "Kamino Finance",
                "description":     "Lending/yield protocol on Solana; KMNOtoken TGE rumoured",
                "estimated_value": "$50-$500",
                "deadline":        "ongoing",
                "program_key":     "Kamino",
                "action":          "Supply/borrow assets on Kamino; vote in governance",
                "url":             "https://app.kamino.finance",
            },
            {
                "protocol":        "Drift Protocol",
                "description":     "Perp DEX; DRIFT token already launched, ongoing rewards",
                "estimated_value": "Trading rewards weekly",
                "deadline":        "ongoing",
                "program_key":     "Drift",
                "action":          "Trade perpetuals; deposit in vaults",
                "url":             "https://app.drift.trade",
            },
            {
                "protocol":        "Marginfi",
                "description":     "Lending protocol; points → token conversion expected",
                "estimated_value": "$100-$2000",
                "deadline":        "ongoing",
                "program_key":     "Marginfi",
                "action":          "Deposit/borrow; earn mrgnfi points daily",
                "url":             "https://app.marginfi.com",
            },
            {
                "protocol":        "Meteora DLMM",
                "description":     "Dynamic liquidity AMM; MET token TGE upcoming",
                "estimated_value": "$200-$3000",
                "deadline":        "ongoing",
                "program_key":     "Meteora",
                "action":          "Provide liquidity in DLMM pools",
                "url":             "https://app.meteora.ag",
            },
            {
                "protocol":        "Pyth Network",
                "description":     "PYTH token ongoing retroactive and staking rewards",
                "estimated_value": "Staking APY + future drops",
                "deadline":        "ongoing",
                "program_key":     "Pyth",
                "action":          "Stake PYTH tokens on pythnetwork.org",
                "url":             "https://staking.pyth.network",
            },
        ]

        for c in candidates:
            prog_key    = c["program_key"]
            act         = activity.get(prog_key)
            interacted  = act and act.tx_count > 0
            eligibility = "interacted" if interacted else "action required"
            action      = ("Already interacted ✓ — continue activity" if interacted
                           else c["action"])
            opportunities.append(AirdropOpportunity(
                protocol        = c["protocol"],
                description     = c["description"],
                estimated_value = c["estimated_value"],
                deadline        = c["deadline"],
                eligibility     = eligibility,
                action_needed   = action,
                source_url      = c["url"],
            ))

        return opportunities

    # ── Reporting ──────────────────────────────────────────────────────────────

    def _print_report(self, opportunities: list[AirdropOpportunity],
                      activity: dict[str, WalletActivity]):
        log.info("── Airdrop Hunter Report ───────────────────────────────────────")
        log.info(f"  Wallet: {self.pubkey}")
        log.info("")
        log.info("  Protocol Interactions:")
        for name, act in activity.items():
            status = f"{act.tx_count} tx" if act.tx_count > 0 else "⚠ NO interactions"
            log.info(f"    {name:<18s}: {status}")
        log.info("")
        log.info("  Airdrop Opportunities:")
        for op in opportunities:
            marker = "✅" if op.eligibility == "interacted" else "🔲"
            log.info(f"  {marker} {op.protocol} ({op.estimated_value})")
            log.info(f"       Status  : {op.eligibility}")
            log.info(f"       Action  : {op.action_needed}")
            log.info(f"       URL     : {op.source_url}")
            log.info("")

    # ── Main loop ──────────────────────────────────────────────────────────────

    def run(self):
        log.info("AirdropHunter running …")
        cycle = 0
        while True:
            try:
                cycle += 1
                log.info(f"AirdropHunter cycle {cycle} — scanning …")
                activity      = self._check_wallet_interactions()
                opportunities = self._build_opportunity_list(activity)
                self._print_report(opportunities, activity)
            except Exception as exc:
                log.error(f"AirdropHunter error: {exc}", exc_info=True)
            time.sleep(3600)   # re-check hourly
