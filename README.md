# Solana SOL Maximizer

A fully automated, modular system for maximizing SOL earnings on Solana.

## Modules

| Module | Strategy | Interval |
|---|---|---|
| **MEVBot** | Cross-DEX triangular arbitrage via Jupiter V6 | 5 s |
| **StakingOptimizer** | Rank validators by APY, recommend rebalancing | 1 hr |
| **NFTArbitrageBot** | Buy floor mispricings on Magic Eden / Tensor | 60 s |
| **AirdropHunter** | Track wallet eligibility across key protocols | 1 hr |
| **ValidatorMonitor** | Real-time validator metrics + alerts | 10 min |

## Setup

```bash
# 1. Clone / copy files into a folder
cd solana_bot

# 2. Create a virtual environment
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure environment
cp .env.example .env
# Edit .env — at minimum set WALLET_PRIVATE_KEY_BASE58

# 5. Run
python core.py
```

## Getting your private key (base58)

```bash
# If you have a Solana CLI keypair JSON file:
python3 -c "
import json, base58
raw = json.load(open('~/.config/solana/id.json'))
print(base58.b58encode(bytes(raw)).decode())
"
```

## Recommended RPC

For production use a paid RPC (Helius, QuickNode, or Triton) and set
`HELIUS_API_KEY` in `.env`.  The free public endpoint will rate-limit you.

- Helius: https://helius.dev
- QuickNode: https://quicknode.com
- Triton: https://triton.one

## Security

- Never commit your `.env` file.
- Run on a dedicated wallet with only the SOL you want to trade.
- Start with small `MAX_TRADE_SOL` (e.g. 0.1) to verify behaviour.

## Architecture

```
core.py  ←  SupervisedThread wrappers  →  auto-restart on crash
    ├── mev_bot.py              Jupiter V6 quote API + swap
    ├── staking_optimizer.py    getVoteAccounts + validators.app
    ├── nft_arbitrage_bot.py    Magic Eden API + Tensor GraphQL
    ├── airdrop_hunter.py       on-chain tx history + protocol list
    └── validator_performance_monitor.py  RPC + tabulate dashboard
```
