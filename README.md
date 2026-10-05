# Meme Coin Momentum Scanner

Polls DEXScreener for tokens on **Robinhood Chain and Solana** (BNB Chain and Base are supported but
off by default; set `enabled = true` under their `[chains.*]` section to add them) and sends a Telegram
alert when a token is **pushed into the $1M–$10M market-cap band by a recent move**: market cap up
sharply in the last few hours, with either a volume spike relative to its own recent average or a
fresh launch.

It is a **pre-screen only**. It flags candidates for manual narrative research. It never buys or
sells, has no wallet integration and does no news or text analysis.

## How it works

Each poll (default every 90s):

1. **Discovery.** DEXScreener has no "list all pairs on a chain" endpoint, so candidates come from:
   - DEXScreener feeds: latest token profiles, recently updated profiles, latest boosts, top
     boosts, community takeovers (plus optional search queries).
   - GeckoTerminal new pools and trending pools (1h) for each chain. This source is much broader
     and can be turned off.

   Candidates under a small discovery floor (default $30k market cap and $5k liquidity) are
   ignored and rechecked every 15 minutes.
2. **Refresh.** Every tracked token is looked up in batches of 30 (`/tokens/v1/{chain}/{addr,...}`).
   Each token uses its most liquid pair.
3. **Snapshot.** Market cap, 1h/6h/24h volume, liquidity and price go into SQLite
   (`data/scanner.db`). DEXScreener has no history, so the bot builds its own. Snapshots are kept
   for 24h (configurable) and survive restarts.
4. **Trigger.** An alert fires only when **all** of these hold:
   - a. market cap is between `market_cap_min` and `market_cap_max` (default $1M–$10M);
   - b. **the move:** market cap is up at least `min_mcap_rise_pct` (default 50%) from its lowest
     point in the last `lookback_hours` (default 3h). The token doesn't need to have started under
     $1M; a push from $2M to $6M counts. This stops tokens that have sat in the band, or are
     sliding, from firing on a volume spike. Set `require_crossover = true` to also require that
     the window low was under `market_cap_min`;
   - c. **either** a **volume spike** (trailing 1h volume ≥ `volume_multiplier`, default 3, × the
     token's trailing 6h hourly average) **or** an **early launch**: the move started within the
     token's first `launch_window_hours` (default 24). Young tokens have no baseline of their own
     to spike against, so the spike test alone misses fast launches. Set
     `launch_window_hours = 0` to disable this path, or `launch_min_volume_to_mcap` (e.g. 0.1)
     to also require 1h volume of at least that fraction of market cap. Alerts are labelled
     🚀 Momentum, 🆕 Early launch, or both;
   - d. liquidity ≥ `min_liquidity_usd` (default $50k), 1h volume ≥ `min_volume_h1_usd` (default off;
     $100k in `config.recommended.toml`) and pair age ≥ `min_pair_age_minutes` (default 15).
5. **Cooldown.** Each token alerts at most once per 6h (configurable).
6. **Pruning.** Tokens are dropped after 24h with no volume, when still under $10k after 2h, when
   above 3× the band top, or when the API has returned no data for them for over an hour.
   There is also a per-chain cap on tracked tokens.

### Details worth knowing

- **Market cap:** uses DEXScreener's `marketCap` when present, otherwise `fdv`. The alert says
  which one was used.
- **Volume baseline:** the 6h hourly average is `volume.h6 / 6`, using DEXScreener's own rolling
  windows. For pairs younger than 6h it is `volume.h6 / age_hours` (age floored at 1h). Without
  that, a 2h-old token would always look like a 3x spike. As a result, a pair under 1 hour old
  can't trigger, because it has no history of its own to spike against.
- **Move on first sight:** a token found while already in the band has no snapshots yet.
  The API's 1h price change gives its market cap an hour ago (assuming constant supply), and that
  value is used as an extra history point, so the rise can be measured straight away. Disable it
  with `use_price_change_inference = false`.
- **Rate limits** (from the docs): 60 req/min for the profile and boost feeds, 300 req/min for
  tokens, pairs and search. The bot runs at 55 and 250, with calls evenly spaced. HTTP 429 and
  5xx responses back off exponentially and honour `Retry-After`. GeckoTerminal's free tier
  returns 429 on bursts well under its documented ~30/min, so the default is 15/min.

### Robinhood Chain

- The chainId slug is **`robinhood`**. This was checked against live DEXScreener responses
  (`https://dexscreener.com/robinhood/...`, `/tokens/v1/robinhood/...` returns pairs) and
  GeckoTerminal (`/networks/robinhood`).
- DEXScreener coverage is good. Robinhood tokens appear throughout the profile and boost feeds,
  so **DEXScreener is the live source for Robinhood Chain**, as it is for every other chain.
  GeckoTerminal is used there for discovery and for backtest history.
- If DEXScreener coverage ever degrades, set `chains.robinhood.source = "geckoterminal"`; that
  live path is implemented and tested. Run `python -m scanner check-sources` to re-verify all
  chain slugs at any time.
- `[chains.robinhood.thresholds]` overrides the trigger for that chain only. The values ship
  equal to the defaults. Tune them with the backtest sweep on Robinhood tokens.

## Setup

### 1. Create the Telegram bot

1. In Telegram, open **@BotFather** and send `/newbot`. Pick a name and a username (must end in
   `bot`). BotFather replies with a **bot token** like `123456789:AA...`. Keep it secret: anyone
   with it can post as your bot. If it leaks, send `/revoke` to BotFather for a new one.
2. Open a chat with your new bot (BotFather links it) and press **Start**, or send it any message.
   For alerts in a group, add the bot to the group and post a message there.
3. Get your **chat ID**:
   ```bash
   TELEGRAM_BOT_TOKEN='123456789:AA...' python -m scanner telegram-chat-id
   ```
   It prints `TELEGRAM_CHAT_ID=...` for every chat that has messaged the bot recently.
4. Check delivery:
   ```bash
   TELEGRAM_BOT_TOKEN='...' TELEGRAM_CHAT_ID='...' python -m scanner test-telegram
   ```

### 2. Install on the VPS

```bash
sudo useradd --system --home /opt/meme-scanner --shell /usr/sbin/nologin scanner
sudo git clone <this repo> /opt/meme-scanner
cd /opt/meme-scanner
sudo python3 -m venv .venv
sudo .venv/bin/pip install -r requirements.txt
sudo cp config.recommended.toml config.toml    # tuned starting point (or config.example.toml for all options)
sudo mkdir -p data logs && sudo chown -R scanner:scanner /opt/meme-scanner
```

Python 3.10+ is required (3.10 installs the small `tomli` package automatically). The only
other runtime dependency is `httpx`.

### 3. Set the env vars (secrets never go in the config file)

```bash
sudo cp deploy/meme-scanner.env.example /etc/meme-scanner.env
sudo nano /etc/meme-scanner.env                # set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID
sudo chmod 600 /etc/meme-scanner.env
```

Test it:

```bash
sudo -u scanner bash -c 'set -a; . /etc/meme-scanner.env; .venv/bin/python -m scanner check-sources'
sudo -u scanner bash -c 'set -a; . /etc/meme-scanner.env; .venv/bin/python -m scanner test-telegram'
sudo -u scanner bash -c 'set -a; . /etc/meme-scanner.env; .venv/bin/python -m scanner once'
```

### 4. Install the systemd service

```bash
sudo cp deploy/meme-scanner.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now meme-scanner
systemctl status meme-scanner
journalctl -u meme-scanner -f          # or: tail -f /opt/meme-scanner/logs/scanner.log
```

The service restarts on crash (`Restart=always`). Logs go to `logs/scanner.log`, rotated at 10 MB
with 5 kept. The bot sends a Telegram message on startup, and a warning after 5 consecutive
failed polls (repeated hourly while it keeps failing, then a "recovered" message).

## Configuration

`config.example.toml` documents every key. The settings are loaded in this order:
1. built-in defaults;
2. `config.toml` (or the file in `SCANNER_CONFIG` / `--config`);
3. env vars.
- `SCANNER_CHAINS=solana,robinhood` enables exactly those chains.
- `SCANNER__<SECTION>__<KEY>=value` overrides any key, for example
  `SCANNER__THRESHOLDS__VOLUME_MULTIPLIER=4` or
  `SCANNER__CHAINS__ROBINHOOD__THRESHOLDS__MIN_LIQUIDITY_USD=30000`.
- `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are read from env only. If they are unset, alerts
  are only logged.

Set `log_level = "DEBUG"` to also log near-misses: tokens that failed exactly one condition.
They are useful for tuning.

## Backtest

```bash
python -m scanner backtest examples/tokens.csv --out backtest_out
python -m scanner backtest runners.csv --controls did_not_run.csv \
    --sweep market_cap_min=500000,1000000 --sweep lookback_hours=2,3,6 --sweep volume_multiplier=2,3,5
```

- **Input:** CSV or JSON with `chain`, `address`, an optional `label` (`runner` / `control`) and an
  optional `pool`. Chains can be written as `solana`, `bsc` / `BNB Chain`, `base`, `robinhood`.
  `--controls FILE` marks every token in that file as a control: a token that did *not* run.
  Controls are used to measure the false-positive rate.
- **History:** comes from GeckoTerminal 5-minute OHLCV of the token's **launch pool** (the earliest
  pool that still has at least $1k liquidity), because today's deepest pool often opened after the
  token had already passed $1M. `--pool-choice liquid` uses the deepest pool instead. History goes back (default
  up to 14 days back, `--max-days`), cached in `data/history_cache/`.
  A token with no history is **listed as `history UNAVAILABLE: <reason>`** in the report, not
  skipped silently.
- **Replay:** the *same* `trigger.evaluate` function the live bot uses runs on every candle
  close, in time order, with the same per-chain thresholds and cooldown.
- **Per token:** fired yes/no, alert time, market cap at alert, peak market cap afterwards (from 5-minute closes, so bad wicks are ignored),
  peak-to-alert multiple, and max drawdown after the alert. Max drawdown is the worst low
  relative to the entry, 0% if price never went below it. Also: return at the end of the horizon
  (`--horizon-hours`, default 24) and the number of alerts.
- **Summary:** hit rate on runners, false-positive rate on controls, median alert-to-peak
  multiple, how many alerts ended below entry, and how many drew down 50% or more.
- **`--sweep`:** grid over any threshold keys. The best combinations are printed and the full
  table goes to `sweep.csv`. History is fetched once and reused for every combination.
- **Outputs:** `report.txt`, `per_token.csv` and `sweep.csv` in `--out`.

**Backtest limitations:**
- **Survivorship bias:** testing only on tokens that pumped measures recall, not precision. Always
  include a control list.
- **Market cap** is reconstructed as price × current supply (constant supply assumed).
- **Liquidity history** isn't available, so the liquidity filter is skipped by default.
  `--current-liquidity` applies today's liquidity, which looks ahead.
- **Polling:** 5-minute candles stand in for a 60–120s poll. Volume is the pool's own, while live
  mode uses DEXScreener's figures for the most liquid pair.

## Commands

| Command | Purpose |
|---|---|
| `python -m scanner run` | polling loop (what systemd runs) |
| `python -m scanner once` | a single poll, then exit |
| `python -m scanner check-sources` | verify each chain's DEXScreener slug and GeckoTerminal coverage |
| `python -m scanner telegram-chat-id` | find your chat ID after messaging the bot |
| `python -m scanner test-telegram` | send a test message |
| `python -m scanner backtest FILE [...]` | backtest / parameter sweep |

## Development

```bash
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest
```
