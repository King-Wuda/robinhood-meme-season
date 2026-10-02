"""CLI entry point: python -m scanner {run,once,check-sources,test-telegram,backtest}."""
from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import sys

from . import backtest
from .config import load_config
from .db import Store
from .poller import Poller, make_api, startup_message
from .sources import dexscreener as ds
from .sources import geckoterminal as gt
from .telegram import Telegram


def setup_logging(log_path: str, level: str) -> None:
    root = logging.getLogger()
    root.setLevel(level.upper())
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(fmt)
    root.addHandler(console)
    if log_path:
        os.makedirs(os.path.dirname(os.path.abspath(log_path)), exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(log_path, maxBytes=10 * 1024 * 1024, backupCount=5)
        fh.setFormatter(fmt)
        root.addHandler(fh)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def check_sources(cfg, api) -> int:
    """Confirm each chain's DEXScreener slug returns pairs and whether GeckoTerminal covers it."""
    ok = True
    profiles = []
    for path in ds.FEEDS:
        try:
            profiles += api.get_json(ds.BASE + path, "ds_feeds") or []
        except Exception as exc:
            print(f"feed {path} failed: {exc}")
    for c in cfg.chains.values():
        sample = next((p["tokenAddress"] for p in profiles if p.get("chainId") == c.dexscreener_id), None)
        ds_pairs = 0
        if sample:
            ds_pairs = len(ds.fetch_tokens(api, c.name, c.dexscreener_id, [sample]))
        else:
            data = api.get_json(ds.BASE + "/latest/dex/search", "ds_pairs", params={"q": c.name}) or {}
            ds_pairs = sum(1 for p in data.get("pairs") or [] if p.get("chainId") == c.dexscreener_id)
        gt_ok = bool(c.geckoterminal_id) and gt.network_exists(api, c.geckoterminal_id)
        print(f"{c.name:<10} dexscreener chainId={c.dexscreener_id!r}: {'OK' if ds_pairs else 'NO PAIRS FOUND'}"
              f" | geckoterminal network={c.geckoterminal_id!r}: {'OK' if gt_ok else 'not available'}"
              f" | live source in use: {c.source}")
        if c.source == "dexscreener" and not ds_pairs:
            ok = False
            print(f"  -> consider setting chains.{c.name}.source = \"geckoterminal\"" if gt_ok else "")
    return 0 if ok else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="scanner", description=__doc__)
    ap.add_argument("--config", help="config TOML (default: $SCANNER_CONFIG or ./config.toml)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="run the polling loop forever")
    sub.add_parser("once", help="run a single poll and exit")
    sub.add_parser("check-sources", help="verify chain slugs / API coverage")
    sub.add_parser("test-telegram", help="send a test message")
    bt = sub.add_parser("backtest", help="replay the trigger over historical data")
    bt.add_argument("tokens", help="CSV or JSON with chain,address[,label][,pool]")
    bt.add_argument("--controls", help="CSV/JSON of tokens that did NOT run (false-positive measurement)")
    bt.add_argument("--out", default="backtest_out", help="output directory")
    bt.add_argument("--sweep", action="append", default=[],
                    help="e.g. --sweep volume_multiplier=2,3,5 --sweep lookback_hours=2,3,6 "
                         "--sweep market_cap_min=500000,1000000")
    bt.add_argument("--horizon-hours", type=float, default=24, help="window after alert for peak/drawdown")
    bt.add_argument("--max-days", type=float, default=14, help="how far back to pull candles")
    bt.add_argument("--current-liquidity", action="store_true",
                    help="apply the liquidity filter using today's liquidity (lookahead)")
    bt.add_argument("--refresh", action="store_true", help="ignore cached history")
    bt.add_argument("--cache-dir", default="data/history_cache")
    args = ap.parse_args(argv)

    cfg = load_config(args.config, include_disabled=args.cmd == "backtest")
    setup_logging(cfg.log_path if args.cmd in ("run", "once") else "", cfg.log_level)
    api = make_api(cfg)

    if args.cmd == "check-sources":
        return check_sources(cfg, api)
    if args.cmd == "test-telegram":
        tg = Telegram()
        if not tg.enabled:
            print("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID first.")
            return 1
        return 0 if tg.send("✅ Scanner test message\n\n" + startup_message(cfg)) else 1
    if args.cmd == "backtest":
        inputs = backtest.load_inputs(args.tokens, cfg)
        if args.controls:
            inputs += backtest.load_inputs(args.controls, cfg, default_label="control")
        backtest.run(cfg, api, inputs, args.out, args.sweep, args.horizon_hours, args.max_days,
                     args.cache_dir, args.refresh, args.current_liquidity)
        return 0

    poller = Poller(cfg, Store(cfg.db_path), api, Telegram())
    if args.cmd == "once":
        errors = poller.run_once()
        for e in errors:
            print("error:", e)
        return 1 if errors else 0
    poller.run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
