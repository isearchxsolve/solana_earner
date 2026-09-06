"""
core.py — System Orchestrator.

Starts all modules in supervised daemon threads.  Monitors thread health
and auto-restarts any module that crashes.  Exposes a simple status loop
so the process stays alive and logs a periodic heartbeat.
"""

import time
import signal
import sys
from threading import Thread, Event
from typing import Callable

from utils import get_logger, get_sol_balance, get_wallet_pubkey

from mev_bot                     import MEVBot
from staking_optimizer            import StakingOptimizer
from nft_arbitrage_bot            import NFTArbitrageBot
from airdrop_hunter               import AirdropHunter
from validator_performance_monitor import ValidatorPerformanceMonitor

log = get_logger("Core")

HEARTBEAT_INTERVAL = 60   # seconds between status prints
RESTART_DELAY      = 10   # seconds before restarting a crashed module


class SupervisedThread:
    """Wraps a module's run() in a thread with auto-restart on crash."""

    def __init__(self, name: str, target: Callable, stop_event: Event):
        self.name        = name
        self.target      = target
        self.stop_event  = stop_event
        self._thread: Thread | None = None
        self.restarts    = 0
        self.last_error  = ""

    def _run_with_supervision(self):
        while not self.stop_event.is_set():
            try:
                log.info(f"[{self.name}] Starting …")
                self.target()
                # If target() returns without stop_event, treat as crash
            except Exception as exc:
                self.last_error = str(exc)
                self.restarts  += 1
                log.error(
                    f"[{self.name}] Crashed (restart #{self.restarts}): {exc}",
                    exc_info=True,
                )
            if not self.stop_event.is_set():
                log.info(f"[{self.name}] Restarting in {RESTART_DELAY}s …")
                self.stop_event.wait(RESTART_DELAY)

    def start(self):
        self._thread = Thread(
            target=self._run_with_supervision,
            name=self.name,
            daemon=True,
        )
        self._thread.start()
        return self

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()


def main():
    log.info("═" * 60)
    log.info("  Solana SOL Maximizer — Starting Up")
    log.info("═" * 60)

    wallet = get_wallet_pubkey()
    balance = get_sol_balance(wallet)
    log.info(f"  Wallet  : {wallet}")
    log.info(f"  Balance : {balance:.4f} SOL")
    log.info("═" * 60)

    # Graceful shutdown event
    stop_event = Event()

    def _shutdown(signum, frame):
        log.info("Shutdown signal received — stopping all modules …")
        stop_event.set()

    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # Instantiate modules
    mev_bot       = MEVBot()
    staking_opt   = StakingOptimizer()
    nft_arb       = NFTArbitrageBot()
    airdrop       = AirdropHunter()
    validator_mon = ValidatorPerformanceMonitor()

    # Supervised threads
    supervised = [
        SupervisedThread("MEVBot",            mev_bot.run,       stop_event).start(),
        SupervisedThread("StakingOptimizer",  staking_opt.run,   stop_event).start(),
        SupervisedThread("NFTArbitrageBot",   nft_arb.run,       stop_event).start(),
        SupervisedThread("AirdropHunter",     airdrop.run,       stop_event).start(),
        SupervisedThread("ValidatorMonitor",  validator_mon.run, stop_event).start(),
    ]

    log.info(f"All {len(supervised)} modules started.")

    # Heartbeat loop
    tick = 0
    while not stop_event.is_set():
        time.sleep(HEARTBEAT_INTERVAL)
        tick += 1
        try:
            balance = get_sol_balance(wallet)
        except Exception:
            balance = -1.0
        alive_count = sum(1 for s in supervised if s.is_alive())
        log.info(
            f"❤  Heartbeat #{tick} | balance={balance:.4f} SOL | "
            f"threads alive={alive_count}/{len(supervised)}"
        )
        for s in supervised:
            status = "✅ alive" if s.is_alive() else "💀 dead"
            log.info(f"   [{s.name:<22s}] {status}  restarts={s.restarts}")

    log.info("All modules stopped. Goodbye.")
    sys.exit(0)


if __name__ == "__main__":
    main()
