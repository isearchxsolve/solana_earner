"""
mev_bot.py — DEX Arbitrage Bot.

Strategy: triangular arbitrage across Solana DEXes (Raydium, Orca, Meteora,
Phoenix, etc.) via Jupiter's aggregator.  For each route defined in config,
we query Jupiter for the best round-trip quote.  If the net output exceeds
the input by at least MIN_PROFIT_SOL (after accounting for tx fees), we build
and submit the two-leg swap atomically via Jupiter's swap API.

This is pure cross-DEX price-discrepancy arbitrage — no sandwich attacks,
no frontrunning of user transactions.
"""

import base64
import time
from typing import Optional

import requests
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction
from solana.rpc.api import Client
from solana.rpc.types import TxOpts
from solana.rpc.commitment import Confirmed

import config
from utils import (
    get_logger, load_keypair, get_client,
    sol_to_lamports, lamports_to_sol, http_get, http_post,
    confirm_transaction, retry,
)

log = get_logger("MEVBot")

# Approximate Solana tx fee in SOL (priority fee + base)
TX_FEE_BUFFER_SOL = 0.000_010  # 10_000 lamports safety margin


class MEVBot:
    """Cross-DEX arbitrage bot using Jupiter V6 API."""

    def __init__(self):
        self.client: Client = get_client()
        self.keypair: Keypair = load_keypair()
        self.pubkey: str = str(self.keypair.pubkey())
        self.trade_amount_lamports: int = sol_to_lamports(config.MAX_TRADE_SOL)
        log.info(f"MEVBot initialised | wallet={self.pubkey} "
                 f"| trade_size={config.MAX_TRADE_SOL} SOL "
                 f"| min_profit={config.MIN_PROFIT_SOL} SOL")

    # ── Quote helpers ──────────────────────────────────────────────────────────

    def _get_quote(self, input_mint: str, output_mint: str,
                   amount: int, slippage_bps: int = config.SLIPPAGE_BPS) -> Optional[dict]:
        """Return Jupiter best-route quote or None on failure."""
        try:
            return http_get(config.JUPITER_QUOTE_URL, params={
                "inputMint":   input_mint,
                "outputMint":  output_mint,
                "amount":      amount,
                "slippageBps": slippage_bps,
                "onlyDirectRoutes": "false",
                "asLegacyTransaction": "false",
            }, timeout=8)
        except Exception as exc:
            log.debug(f"Quote failed {input_mint[:6]}→{output_mint[:6]}: {exc}")
            return None

    def _check_arbitrage(self, leg1_in_mint: str, leg1_out_mint: str,
                         leg2_in_mint: str, leg2_out_mint: str,
                         amount_lamports: int) -> Optional[tuple[dict, dict, float]]:
        """
        Evaluate a two-leg round-trip route.
        Returns (quote1, quote2, profit_sol) if profitable, else None.
        """
        q1 = self._get_quote(leg1_in_mint, leg1_out_mint, amount_lamports)
        if not q1:
            return None

        # Use actual output of leg1 as input to leg2
        mid_amount = int(q1.get("outAmount", 0))
        if mid_amount == 0:
            return None

        q2 = self._get_quote(leg2_in_mint, leg2_out_mint, mid_amount)
        if not q2:
            return None

        final_amount = int(q2.get("outAmount", 0))
        profit_lamports = final_amount - amount_lamports - sol_to_lamports(TX_FEE_BUFFER_SOL)
        profit_sol = lamports_to_sol(profit_lamports)
        return (q1, q2, profit_sol)

    # ── Swap execution ─────────────────────────────────────────────────────────

    @retry(max_attempts=2, delay=1.0)
    def _build_swap_tx(self, quote: dict) -> Optional[bytes]:
        """Ask Jupiter to build the swap transaction and return raw bytes."""
        body = {
            "quoteResponse":              quote,
            "userPublicKey":              self.pubkey,
            "wrapAndUnwrapSol":           True,
            "computeUnitPriceMicroLamports": 100_000,   # priority fee
            "asLegacyTransaction":        False,
        }
        resp = http_post(config.JUPITER_SWAP_URL, json_body=body, timeout=15)
        tx_b64 = resp.get("swapTransaction")
        if not tx_b64:
            raise ValueError("No swapTransaction in Jupiter response")
        return base64.b64decode(tx_b64)

    def _sign_and_send(self, raw_tx: bytes) -> Optional[str]:
        """Deserialise, sign, and submit a VersionedTransaction."""
        try:
            tx = VersionedTransaction.from_bytes(raw_tx)
            # Sign the transaction with our keypair
            tx.sign([self.keypair])
            sig_resp = self.client.send_raw_transaction(
                bytes(tx),
                opts=TxOpts(skip_preflight=False, preflight_commitment=Confirmed),
            )
            return str(sig_resp.value)
        except Exception as exc:
            log.error(f"Send transaction failed: {exc}")
            return None

    def _execute_arbitrage(self, q1: dict, q2: dict, profit_sol: float) -> bool:
        """Build, sign, and send both swap legs sequentially."""
        log.info(f"⚡ Executing arbitrage | expected profit={profit_sol:.6f} SOL")

        # Leg 1
        raw1 = self._build_swap_tx(q1)
        if not raw1:
            return False
        sig1 = self._sign_and_send(raw1)
        if not sig1:
            return False
        log.info(f"  Leg 1 submitted: {sig1}")

        # Wait for leg 1 to confirm before sending leg 2
        if not confirm_transaction(sig1, self.client, timeout=30):
            log.error("  Leg 1 failed to confirm — aborting leg 2")
            return False

        # Leg 2
        raw2 = self._build_swap_tx(q2)
        if not raw2:
            return False
        sig2 = self._sign_and_send(raw2)
        if not sig2:
            return False
        log.info(f"  Leg 2 submitted: {sig2}")
        confirmed = confirm_transaction(sig2, self.client, timeout=30)
        if confirmed:
            log.info(f"✅ Arbitrage complete | profit≈{profit_sol:.6f} SOL | "
                     f"sig1={sig1[:20]}… sig2={sig2[:20]}…")
        else:
            log.warning("  Leg 2 failed to confirm")
        return confirmed

    # ── Main loop ──────────────────────────────────────────────────────────────

    def scan_once(self) -> int:
        """Scan all configured routes once.  Returns number of trades executed."""
        executed = 0
        for (a_mint, b_mint, c_mint) in config.ARBI_ROUTES:
            result = self._check_arbitrage(
                a_mint, b_mint, b_mint, c_mint,
                self.trade_amount_lamports,
            )
            if result is None:
                continue
            q1, q2, profit_sol = result
            if profit_sol >= config.MIN_PROFIT_SOL:
                log.info(f"🎯 Opportunity found: {a_mint[:6]}→{b_mint[:6]}→{c_mint[:6]} "
                         f"profit={profit_sol:.6f} SOL")
                if self._execute_arbitrage(q1, q2, profit_sol):
                    executed += 1
            else:
                log.debug(f"  {a_mint[:6]}→{b_mint[:6]}→{c_mint[:6]} "
                          f"profit={profit_sol:.6f} SOL (below threshold)")
        return executed

    def run(self):
        """Continuous scan loop."""
        log.info("MEVBot running …")
        cycle = 0
        while True:
            try:
                cycle += 1
                trades = self.scan_once()
                log.info(f"Cycle {cycle} complete | trades={trades}")
            except Exception as exc:
                log.error(f"MEVBot cycle error: {exc}", exc_info=True)
            time.sleep(5)   # 5-second scan interval
