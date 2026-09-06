"""
config.py — Central configuration loader.
All modules import from here; never hardcode values.
"""
import os
from dotenv import load_dotenv

load_dotenv()

# ── Wallet ──────────────────────────────────────────────────────────────────
WALLET_PRIVATE_KEY_BASE58: str = os.environ["WALLET_PRIVATE_KEY_BASE58"]

# ── RPC ─────────────────────────────────────────────────────────────────────
SOLANA_RPC_URL: str = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
SOLANA_WS_URL:  str = os.getenv("SOLANA_WS_URL",  "wss://api.mainnet-beta.solana.com")
HELIUS_API_KEY: str = os.getenv("HELIUS_API_KEY", "")

# Construct enhanced RPC URL if Helius key is provided
if HELIUS_API_KEY:
    SOLANA_RPC_URL = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
    SOLANA_WS_URL  = f"wss://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"

# ── Arbitrage ────────────────────────────────────────────────────────────────
MIN_PROFIT_SOL:  float = float(os.getenv("MIN_PROFIT_SOL",  "0.001"))
MAX_TRADE_SOL:   float = float(os.getenv("MAX_TRADE_SOL",   "1.0"))
SLIPPAGE_BPS:    int   = int(os.getenv("SLIPPAGE_BPS",      "50"))

# ── NFT ──────────────────────────────────────────────────────────────────────
NFT_MIN_PROFIT_SOL: float = float(os.getenv("NFT_MIN_PROFIT_SOL", "0.05"))
NFT_MAX_BUDGET_SOL: float = float(os.getenv("NFT_MAX_BUDGET_SOL", "5.0"))

# ── Staking ──────────────────────────────────────────────────────────────────
STAKE_REBALANCE_THRESHOLD: float = float(os.getenv("STAKE_REBALANCE_THRESHOLD", "0.5"))
MAX_VALIDATORS:            int   = int(os.getenv("MAX_VALIDATORS", "5"))

# ── Monitoring ───────────────────────────────────────────────────────────────
DASHBOARD_REFRESH_SECONDS: int = int(os.getenv("DASHBOARD_REFRESH_SECONDS", "60"))
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

# ── Jupiter API ──────────────────────────────────────────────────────────────
JUPITER_QUOTE_URL = "https://quote-api.jup.ag/v6/quote"
JUPITER_SWAP_URL  = "https://quote-api.jup.ag/v6/swap"
JUPITER_PRICE_URL = "https://price.jup.ag/v4/price"

# ── Magic Eden API ───────────────────────────────────────────────────────────
MAGIC_EDEN_API   = "https://api-mainnet.magiceden.dev/v2"
TENSOR_API       = "https://api.tensor.so/graphql"

# ── Token mints used for arbitrage routes ────────────────────────────────────
SOL_MINT  = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
BONK_MINT = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
JTO_MINT  = "jtojtomepa8b1tDPp6cW7KE1suDmJ5vzqhEfQEoUEXT8"
WIF_MINT  = "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm"
RAY_MINT  = "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R"
ORCA_MINT = "orcaEKTdK7LKz57vaAYr9QeNsVEPfiu6QeMU1kektZE"

# Arbitrage triangle routes to monitor  (start_mint → mid_mint → start_mint)
ARBI_ROUTES = [
    (SOL_MINT,  USDC_MINT, SOL_MINT),
    (SOL_MINT,  USDT_MINT, SOL_MINT),
    (SOL_MINT,  BONK_MINT, SOL_MINT),
    (SOL_MINT,  JTO_MINT,  SOL_MINT),
    (SOL_MINT,  WIF_MINT,  SOL_MINT),
    (SOL_MINT,  RAY_MINT,  SOL_MINT),
    (SOL_MINT,  ORCA_MINT, SOL_MINT),
    (USDC_MINT, USDT_MINT, USDC_MINT),
]

# Popular NFT collections to monitor on Magic Eden
NFT_COLLECTIONS = [
    "mad_lads",
    "tensorians",
    "y00ts",
    "degenerate_ape_academy",
    "okay_bears",
    "famous_fox_federation",
    "solana_monkey_business",
]
