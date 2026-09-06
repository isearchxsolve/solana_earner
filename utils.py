"""
utils.py — Shared helpers: wallet, RPC wrappers, logging setup, retry logic.
"""
import base58
import logging
import time
import functools
import requests
from typing import Any, Callable, Optional

import colorlog
from solders.keypair import Keypair as SoldersKeypair
from solana.rpc.api import Client
from solana.rpc.commitment import Confirmed, Finalized

import config


# ── Logging ──────────────────────────────────────────────────────────────────

def get_logger(name: str) -> logging.Logger:
    handler = colorlog.StreamHandler()
    handler.setFormatter(colorlog.ColoredFormatter(
        "%(log_color)s%(asctime)s [%(name)s] %(levelname)s%(reset)s  %(message)s",
        datefmt="%H:%M:%S",
        log_colors={
            "DEBUG":    "cyan",
            "INFO":     "green",
            "WARNING":  "yellow",
            "ERROR":    "red",
            "CRITICAL": "bold_red",
        },
    ))
    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, config.LOG_LEVEL, logging.INFO))
    if not logger.handlers:
        logger.addHandler(handler)
    return logger


log = get_logger("utils")


# ── Wallet helpers ────────────────────────────────────────────────────────────

def load_keypair() -> SoldersKeypair:
    """Load the wallet keypair from the base58 private key in config."""
    raw = base58.b58decode(config.WALLET_PRIVATE_KEY_BASE58)
    return SoldersKeypair.from_bytes(raw)


def get_wallet_pubkey() -> str:
    return str(load_keypair().pubkey())


# ── RPC client ───────────────────────────────────────────────────────────────

def get_client() -> Client:
    return Client(config.SOLANA_RPC_URL)


# ── Retry decorator ───────────────────────────────────────────────────────────

def retry(max_attempts: int = 3, delay: float = 2.0, backoff: float = 2.0,
          exceptions: tuple = (Exception,)):
    """Retry a function up to max_attempts times with exponential backoff."""
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs) -> Any:
            attempt = 0
            wait = delay
            while attempt < max_attempts:
                try:
                    return func(*args, **kwargs)
                except exceptions as exc:
                    attempt += 1
                    if attempt >= max_attempts:
                        log.error(f"{func.__name__} failed after {max_attempts} attempts: {exc}")
                        raise
                    log.warning(f"{func.__name__} attempt {attempt} failed ({exc}); "
                                f"retrying in {wait:.1f}s …")
                    time.sleep(wait)
                    wait *= backoff
        return wrapper
    return decorator


# ── SOL / lamport conversions ─────────────────────────────────────────────────

LAMPORTS_PER_SOL = 1_000_000_000

def sol_to_lamports(sol: float) -> int:
    return int(sol * LAMPORTS_PER_SOL)

def lamports_to_sol(lamports: int) -> float:
    return lamports / LAMPORTS_PER_SOL


# ── Generic HTTP GET with retries ─────────────────────────────────────────────

@retry(max_attempts=3, delay=1.0, exceptions=(requests.RequestException,))
def http_get(url: str, params: Optional[dict] = None,
             headers: Optional[dict] = None, timeout: int = 10) -> Any:
    resp = requests.get(url, params=params, headers=headers, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


@retry(max_attempts=3, delay=1.0, exceptions=(requests.RequestException,))
def http_post(url: str, json_body: dict,
              headers: Optional[dict] = None, timeout: int = 10) -> Any:
    resp = requests.post(url, json=json_body, headers=headers, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


# ── Solana RPC helpers ────────────────────────────────────────────────────────

def get_sol_balance(pubkey: str, client: Optional[Client] = None) -> float:
    """Return SOL balance of a public key."""
    c = client or get_client()
    resp = c.get_balance(pubkey, commitment=Confirmed)
    return lamports_to_sol(resp.value)


def get_token_balance(pubkey: str, mint: str,
                      client: Optional[Client] = None) -> float:
    """Return SPL token balance for a given mint."""
    c = client or get_client()
    resp = c.get_token_accounts_by_owner(
        pubkey,
        {"mint": mint},
        commitment=Confirmed,
    )
    if not resp.value:
        return 0.0
    token_account = resp.value[0].pubkey
    bal = c.get_token_account_balance(token_account, commitment=Confirmed)
    return float(bal.value.ui_amount or 0)


def get_slot(client: Optional[Client] = None) -> int:
    c = client or get_client()
    return c.get_slot(commitment=Confirmed).value


def confirm_transaction(sig: str, client: Optional[Client] = None,
                        timeout: int = 60) -> bool:
    """Poll until a transaction is confirmed or timeout expires."""
    c = client or get_client()
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            result = c.get_signature_statuses([sig])
            status = result.value[0]
            if status and status.confirmation_status in ("confirmed", "finalized"):
                return status.err is None
        except Exception:
            pass
        time.sleep(2)
    return False
