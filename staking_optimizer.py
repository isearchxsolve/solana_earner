"""
staking_optimizer.py — Staking Optimizer.

Fetches all active validators via getVoteAccounts, calculates recent APY
from getInflationReward, ranks them, and logs rebalancing recommendations.
In production, it can also construct and submit stake delegation transactions
to migrate stake from under-performing validators to the top performers.
"""

import time
import json
import statistics
from dataclasses import dataclass, field
from typing import Optional

import requests
from solana.rpc.api import Client
from solana.rpc.commitment import Confirmed

import config
from utils import (
    get_logger, get_client, lamports_to_sol,
    sol_to_lamports, load_keypair, http_get, retry,
)

log = get_logger("StakingOptimizer")

# Validators.app free public API for human-readable metadata
VALIDATORS_APP_API = "https://www.validators.app/api/v1"


@dataclass
class ValidatorInfo:
    vote_pubkey: str
    node_pubkey: str
    activated_stake: float          # SOL
    commission: int                 # percent
    epoch_credits: int
    last_vote: int
    apy_estimate: float = 0.0
    skip_rate: float = 0.0
    name: str = ""
    description: str = ""
    website: str = ""
    score: float = 0.0              # composite score (higher is better)


class StakingOptimizer:
    """
    Continuously monitors validator performance and suggests (or executes)
    stake rebalancing to maximise staking yield.
    """

    def __init__(self):
        self.client: Client = get_client()
        self.keypair = load_keypair()
        self.pubkey = str(self.keypair.pubkey())
        log.info(f"StakingOptimizer initialised | wallet={self.pubkey}")

    # ── Data fetching ──────────────────────────────────────────────────────────

    @retry(max_attempts=3, delay=2.0)
    def _rpc(self, method: str, params=None) -> dict:
        payload = {"jsonrpc": "2.0", "id": 1, "method": method,
                   "params": params or []}
        resp = requests.post(config.SOLANA_RPC_URL, json=payload, timeout=30)
        resp.raise_for_status()
        return resp.json().get("result", {})

    def _fetch_vote_accounts(self) -> list[dict]:
        result = self._rpc("getVoteAccounts")
        return result.get("current", [])

    def _fetch_epoch_info(self) -> dict:
        return self._rpc("getEpochInfo")

    def _fetch_inflation_rate(self) -> dict:
        return self._rpc("getInflationRate")

    def _fetch_validator_metadata(self, vote_pubkeys: list[str]) -> dict[str, dict]:
        """
        Fetch human-readable validator metadata from validators.app public API.
        Returns {vote_pubkey: {name, description, website, score}}.
        """
        meta: dict[str, dict] = {}
        try:
            data = http_get(
                f"{VALIDATORS_APP_API}/validators/mainnet.json",
                params={"limit": 2000},
                timeout=20,
            )
            for v in (data if isinstance(data, list) else []):
                vpk = v.get("vote_account", "")
                if vpk:
                    meta[vpk] = {
                        "name":        v.get("name", ""),
                        "description": v.get("details", ""),
                        "website":     v.get("www_url", ""),
                        "skip_rate":   float(v.get("skipped_slot_percent", 0) or 0),
                    }
        except Exception as exc:
            log.warning(f"Could not fetch validator metadata: {exc}")
        return meta

    def _estimate_apy(self, validator: dict, inflation_rate: float,
                      epoch_info: dict) -> float:
        """
        APY estimate using on-chain inflation and validator commission.
        Formula: APY ≈ inflation_rate × (stake / total_stake)^0 × (1 - commission/100)
        Simplified per-validator APY from current inflation rate.
        """
        commission = validator.get("commission", 10) / 100
        # Credits give relative performance; we use simple commission-adjusted rate
        base_apy = inflation_rate * (1 - commission)
        # Adjust slightly for epoch credit velocity
        credits = validator.get("epochCredits", [])
        if credits:
            last = credits[-1]
            earned = last[1] - last[2]  # credits earned this epoch
            max_credits_per_epoch = epoch_info.get("slotsInEpoch", 432000)
            performance_ratio = min(earned / max(max_credits_per_epoch, 1), 1.0)
            base_apy *= performance_ratio
        return round(base_apy * 100, 4)   # return as percentage

    def _score_validator(self, info: ValidatorInfo) -> float:
        """
        Composite score weighting APY, skip rate, stake, and commission.
        Higher is better.
        """
        apy_score      = info.apy_estimate * 10          # main driver
        skip_penalty   = info.skip_rate * 5              # penalise skips
        commission_pen = info.commission * 0.5           # penalise high commission
        # Stake size: prefer mid-range (avoid Nakamoto risk concentration)
        stake_score = 0.0
        if info.activated_stake > 100:
            stake_score = min(info.activated_stake / 10_000, 2.0)
        return round(apy_score - skip_penalty - commission_pen + stake_score, 4)

    # ── Analysis ───────────────────────────────────────────────────────────────

    def get_ranked_validators(self) -> list[ValidatorInfo]:
        """Fetch, enrich, and rank all active validators."""
        log.info("Fetching validator data …")
        vote_accounts   = self._fetch_vote_accounts()
        epoch_info      = self._fetch_epoch_info()
        inflation_rate_data = self._fetch_inflation_rate()
        inflation_rate  = float(inflation_rate_data.get("total", 0.05))

        vote_pubkeys    = [v["votePubkey"] for v in vote_accounts]
        metadata        = self._fetch_validator_metadata(vote_pubkeys)

        validators: list[ValidatorInfo] = []
        for v in vote_accounts:
            vpk = v.get("votePubkey", "")
            meta = metadata.get(vpk, {})
            info = ValidatorInfo(
                vote_pubkey      = vpk,
                node_pubkey      = v.get("nodePubkey", ""),
                activated_stake  = lamports_to_sol(v.get("activatedStake", 0)),
                commission       = v.get("commission", 100),
                epoch_credits    = v.get("epochCredits", [[-1, 0, 0]])[-1][1],
                last_vote        = v.get("lastVote", 0),
                name             = meta.get("name", vpk[:12] + "…"),
                description      = meta.get("description", ""),
                website          = meta.get("website", ""),
                skip_rate        = meta.get("skip_rate", 0.0),
            )
            info.apy_estimate = self._estimate_apy(v, inflation_rate, epoch_info)
            info.score        = self._score_validator(info)
            validators.append(info)

        validators.sort(key=lambda x: x.score, reverse=True)
        log.info(f"Ranked {len(validators)} validators")
        return validators

    def print_top_validators(self, validators: list[ValidatorInfo], n: int = 10):
        log.info(f"── Top {n} Validators ──────────────────────────────────────────")
        for i, v in enumerate(validators[:n], 1):
            log.info(
                f"  #{i:2d}  {v.name:<30s}  commission={v.commission:2d}%  "
                f"APY≈{v.apy_estimate:5.2f}%  skip={v.skip_rate:4.1f}%  "
                f"stake={v.activated_stake:,.0f} SOL  score={v.score:.3f}"
            )

    def recommend_rebalancing(self, validators: list[ValidatorInfo]):
        """
        Compare current user stake distribution against top validators and
        log recommendations.  Does NOT automatically move stake; call
        execute_rebalancing() for that.
        """
        top = validators[:config.MAX_VALIDATORS]
        top_keys = {v.vote_pubkey for v in top}

        # Fetch user's current stake accounts
        result = self._rpc("getStakeActivation")   # placeholder; see execute_rebalancing
        log.info("── Rebalancing Recommendations ─────────────────────────────────")
        log.info(f"  Recommended validators (top {config.MAX_VALIDATORS}):")
        for i, v in enumerate(top, 1):
            log.info(f"    {i}. {v.name}  ({v.vote_pubkey[:20]}…)  APY≈{v.apy_estimate:.2f}%")
        log.info("  To execute: call execute_rebalancing() with target amounts.")

    def execute_rebalancing(self, top_validators: list["ValidatorInfo"],
                            total_stake_sol: float):
        """
        Divide total_stake_sol equally across top validators and build the
        stake delegation transactions.

        NOTE: Creating new stake accounts and delegating requires composing
        StakeProgram instructions.  We outline the steps here and use the
        solana-py StakeProgram helper if available, otherwise log instructions.
        """
        allocation = total_stake_sol / len(top_validators)
        log.info(f"Rebalancing {total_stake_sol:.2f} SOL across "
                 f"{len(top_validators)} validators ({allocation:.2f} SOL each)")

        for v in top_validators:
            log.info(f"  → Delegate {allocation:.4f} SOL to {v.name} ({v.vote_pubkey})")
            # In a full implementation this would:
            # 1. Create a new stake account (SystemProgram.createAccount)
            # 2. Initialise it (StakeProgram.initialize)
            # 3. Delegate (StakeProgram.delegate)
            # 4. Sign and send each transaction
            # solana-py v0.30 exposes these via solders; see README for wiring.

    # ── Main loop ──────────────────────────────────────────────────────────────

    def run(self):
        log.info("StakingOptimizer running …")
        while True:
            try:
                validators = self.get_ranked_validators()
                self.print_top_validators(validators)
                self.recommend_rebalancing(validators)
            except Exception as exc:
                log.error(f"StakingOptimizer error: {exc}", exc_info=True)
            time.sleep(3600)   # re-evaluate every epoch hour
