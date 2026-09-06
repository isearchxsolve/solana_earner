"""
nft_arbitrage_bot.py — NFT Arbitrage Bot.

Strategy:
  1. Fetch floor prices for target collections from both Magic Eden and Tensor.
  2. Identify individual listings priced below the floor on the OTHER marketplace.
  3. If (floor_price_B - listing_price_A - fees) > NFT_MIN_PROFIT_SOL, buy on A
     and immediately list at floor on B.

This exploits temporary price discrepancies and listing mispricings across
the two largest Solana NFT marketplaces.
"""

import time
from dataclasses import dataclass
from typing import Optional

import requests

import config
from utils import (
    get_logger, get_client, load_keypair,
    sol_to_lamports, lamports_to_sol,
    http_get, http_post, retry,
)

log = get_logger("NFTArbitrageBot")

# Marketplace fees (taker side, %)
MAGIC_EDEN_FEE_PCT = 0.02   # 2%
TENSOR_FEE_PCT     = 0.015  # 1.5%

# Magic Eden API headers
ME_HEADERS = {"accept": "application/json"}


@dataclass
class NFTListing:
    mint:          str
    marketplace:   str          # "magiceden" | "tensor"
    price_sol:     float
    seller:        str
    collection:    str
    token_address: str = ""


@dataclass
class CollectionFloor:
    collection:     str
    me_floor_sol:   float   = 0.0
    tensor_floor_sol: float = 0.0

    @property
    def best_floor(self) -> float:
        return max(self.me_floor_sol, self.tensor_floor_sol)

    @property
    def best_floor_market(self) -> str:
        return "magiceden" if self.me_floor_sol >= self.tensor_floor_sol else "tensor"


class NFTArbitrageBot:
    """Scan NFT marketplaces for cross-platform price discrepancies."""

    def __init__(self):
        self.client  = get_client()
        self.keypair = load_keypair()
        self.pubkey  = str(self.keypair.pubkey())
        log.info(f"NFTArbitrageBot initialised | wallet={self.pubkey} "
                 f"| budget={config.NFT_MAX_BUDGET_SOL} SOL "
                 f"| min_profit={config.NFT_MIN_PROFIT_SOL} SOL")

    # ── Magic Eden helpers ─────────────────────────────────────────────────────

    @retry(max_attempts=3, delay=2.0)
    def _me_floor_price(self, collection_symbol: str) -> float:
        """Return Magic Eden floor price in SOL for a collection."""
        data = http_get(
            f"{config.MAGIC_EDEN_API}/collections/{collection_symbol}/stats",
            headers=ME_HEADERS,
        )
        floor_lamports = data.get("floorPrice", 0) or 0
        return lamports_to_sol(floor_lamports)

    @retry(max_attempts=3, delay=2.0)
    def _me_listings(self, collection_symbol: str,
                     limit: int = 20) -> list[NFTListing]:
        """Return cheapest listings from Magic Eden for a collection."""
        data = http_get(
            f"{config.MAGIC_EDEN_API}/collections/{collection_symbol}/listings",
            params={"offset": 0, "limit": limit},
            headers=ME_HEADERS,
        )
        listings = []
        for item in (data or []):
            price_sol = item.get("price", 0) or 0
            if price_sol <= 0:
                continue
            listings.append(NFTListing(
                mint        = item.get("tokenMint", ""),
                marketplace = "magiceden",
                price_sol   = float(price_sol),
                seller      = item.get("seller", ""),
                collection  = collection_symbol,
                token_address = item.get("tokenAddress", ""),
            ))
        return listings

    @retry(max_attempts=3, delay=2.0)
    def _me_buy_now(self, listing: NFTListing) -> Optional[str]:
        """
        Fetch a Magic Eden buy-now transaction, sign it, and submit it.
        Returns the transaction signature on success.
        """
        resp = http_get(
            f"{config.MAGIC_EDEN_API}/instructions/buy_now",
            params={
                "buyer":              self.pubkey,
                "seller":             listing.seller,
                "auctionHouseAddress": "E8cU1WiRWjanGxmn96ewBgk9vPTcL6AEZ1t6F6fkgUWe",
                "tokenMint":          listing.mint,
                "tokenATA":           listing.token_address,
                "price":              listing.price_sol,
                "sellerReferral":     "",
                "sellerExpiry":       -1,
            },
            headers=ME_HEADERS,
        )
        tx_data = resp.get("tx", {}).get("data")
        if not tx_data:
            log.warning(f"ME buy_now: no tx data for mint {listing.mint}")
            return None
        return self._sign_and_send_raw(bytes(tx_data))

    # ── Tensor helpers ─────────────────────────────────────────────────────────

    TENSOR_FLOOR_QUERY = """
    query CollectionFloor($slug: String!) {
      instrumentTV2(slug: $slug) {
        statsV2 { sellNowPrice }
      }
    }
    """

    TENSOR_LISTINGS_QUERY = """
    query Listings($slug: String!, $limit: Int!) {
      tcompListings(slug: $slug, sortBy: PriceAsc, limit: $limit) {
        txs {
          tx { mintOnchainId sellerAddress grossAmount }
        }
      }
    }
    """

    @retry(max_attempts=3, delay=2.0)
    def _tensor_graphql(self, query: str, variables: dict) -> dict:
        resp = requests.post(
            config.TENSOR_API,
            json={"query": query, "variables": variables},
            headers={"Content-Type": "application/json"},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json().get("data", {})

    def _tensor_floor_price(self, slug: str) -> float:
        data = self._tensor_graphql(self.TENSOR_FLOOR_QUERY, {"slug": slug})
        raw = (data.get("instrumentTV2") or {}).get("statsV2", {}).get("sellNowPrice") or 0
        return lamports_to_sol(int(raw))

    def _tensor_listings(self, slug: str, limit: int = 20) -> list[NFTListing]:
        data = self._tensor_graphql(self.TENSOR_LISTINGS_QUERY,
                                    {"slug": slug, "limit": limit})
        listings = []
        for entry in (data.get("tcompListings") or {}).get("txs", []):
            tx = entry.get("tx", {})
            price_sol = lamports_to_sol(int(tx.get("grossAmount", 0)))
            if price_sol <= 0:
                continue
            listings.append(NFTListing(
                mint        = tx.get("mintOnchainId", ""),
                marketplace = "tensor",
                price_sol   = price_sol,
                seller      = tx.get("sellerAddress", ""),
                collection  = slug,
            ))
        return listings

    # ── Transaction signing ────────────────────────────────────────────────────

    def _sign_and_send_raw(self, raw_tx: bytes) -> Optional[str]:
        from solders.transaction import VersionedTransaction
        from solana.rpc.types import TxOpts
        from solana.rpc.commitment import Confirmed
        try:
            tx = VersionedTransaction.from_bytes(raw_tx)
            tx.sign([self.keypair])
            resp = self.client.send_raw_transaction(
                bytes(tx),
                opts=TxOpts(skip_preflight=False, preflight_commitment=Confirmed),
            )
            return str(resp.value)
        except Exception as exc:
            log.error(f"Sign/send failed: {exc}")
            return None

    # ── Core arbitrage logic ───────────────────────────────────────────────────

    def _get_collection_floors(self, collection: str) -> CollectionFloor:
        cf = CollectionFloor(collection=collection)
        try:
            cf.me_floor_sol     = self._me_floor_price(collection)
        except Exception as exc:
            log.debug(f"ME floor fetch failed for {collection}: {exc}")
        try:
            cf.tensor_floor_sol = self._tensor_floor_price(collection)
        except Exception as exc:
            log.debug(f"Tensor floor fetch failed for {collection}: {exc}")
        return cf

    def _profit_after_fees(self, buy_price: float, sell_floor: float,
                           buy_market: str, sell_market: str) -> float:
        """Net profit accounting for marketplace taker fees on both sides."""
        buy_fee  = buy_price  * (MAGIC_EDEN_FEE_PCT if buy_market  == "magiceden" else TENSOR_FEE_PCT)
        sell_fee = sell_floor * (MAGIC_EDEN_FEE_PCT if sell_market == "magiceden" else TENSOR_FEE_PCT)
        return sell_floor - buy_price - buy_fee - sell_fee

    def _scan_collection(self, collection: str) -> int:
        """Scan one collection for arbitrage opportunities. Returns trades executed."""
        cf = self._get_collection_floors(collection)
        if cf.me_floor_sol == 0 and cf.tensor_floor_sol == 0:
            return 0

        log.debug(f"{collection}: ME floor={cf.me_floor_sol:.4f} SOL | "
                  f"Tensor floor={cf.tensor_floor_sol:.4f} SOL")

        trades = 0

        # --- Strategy A: buy cheapest ME listing, sell at Tensor floor ---
        if cf.tensor_floor_sol > cf.me_floor_sol > 0:
            me_listings = self._me_listings(collection, limit=10)
            for listing in me_listings:
                if listing.price_sol > config.NFT_MAX_BUDGET_SOL:
                    continue
                profit = self._profit_after_fees(
                    listing.price_sol, cf.tensor_floor_sol,
                    "magiceden", "tensor",
                )
                if profit >= config.NFT_MIN_PROFIT_SOL:
                    log.info(
                        f"🖼  NFT Arb | collection={collection} | "
                        f"buy={listing.price_sol:.4f} SOL on ME | "
                        f"sell≈{cf.tensor_floor_sol:.4f} SOL on Tensor | "
                        f"profit≈{profit:.4f} SOL | mint={listing.mint[:12]}…"
                    )
                    sig = self._me_buy_now(listing)
                    if sig:
                        log.info(f"  Purchased! sig={sig[:20]}… | "
                                 f"Now manually list on Tensor at floor.")
                        trades += 1
                    break   # one purchase per collection per cycle

        # --- Strategy B: buy cheapest Tensor listing, sell at ME floor ---
        elif cf.me_floor_sol > cf.tensor_floor_sol > 0:
            tensor_listings = self._tensor_listings(collection, limit=10)
            for listing in tensor_listings:
                if listing.price_sol > config.NFT_MAX_BUDGET_SOL:
                    continue
                profit = self._profit_after_fees(
                    listing.price_sol, cf.me_floor_sol,
                    "tensor", "magiceden",
                )
                if profit >= config.NFT_MIN_PROFIT_SOL:
                    log.info(
                        f"🖼  NFT Arb | collection={collection} | "
                        f"buy={listing.price_sol:.4f} SOL on Tensor | "
                        f"sell≈{cf.me_floor_sol:.4f} SOL on ME | "
                        f"profit≈{profit:.4f} SOL | mint={listing.mint[:12]}…"
                    )
                    # Tensor buy requires their SDK / direct contract calls;
                    # log the opportunity for manual or SDK-based execution.
                    log.warning("  Tensor buy-now requires Tensor SDK integration "
                                "(see docs.tensor.trade). Logging opportunity.")
                    break

        return trades

    def run(self):
        log.info("NFTArbitrageBot running …")
        cycle = 0
        while True:
            try:
                cycle += 1
                total_trades = 0
                for collection in config.NFT_COLLECTIONS:
                    total_trades += self._scan_collection(collection)
                log.info(f"NFT cycle {cycle} complete | trades={total_trades}")
            except Exception as exc:
                log.error(f"NFTArbitrageBot error: {exc}", exc_info=True)
            time.sleep(60)   # scan every minute
