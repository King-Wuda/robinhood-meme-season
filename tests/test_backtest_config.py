from scanner.backtest import History, load_inputs, parse_sweep, replay, summarize
from scanner.config import load_config
from scanner.trigger import HOUR, Thresholds

T0 = 1_800_000_000


def synthetic_history():
    """Pool born at T0. 8h flat at $500k with low volume, then a pump to $3M with heavy volume,
    then a fade. Supply 1e9 so mcap = price * 1e9."""
    candles = []
    for i in range(int(14 * HOUR / 300)):
        ts = T0 + i * 300
        if ts < T0 + 8 * HOUR:
            price, vol = 0.0005, 500
        elif ts < T0 + 9 * HOUR:
            price, vol = 0.0005 + 0.0025 * (ts - T0 - 8 * HOUR) / HOUR, 20_000
        else:
            price, vol = 0.002, 2_000
        candles.append([ts, price, price * 1.05, price * 0.95, price, vol])
    return History("solana", "tok", pool="pool", symbol="TST", supply=1e9, mcap_source="FDV",
                   created_at=T0, complete=True, candles=candles)


def test_replay_fires_once_and_measures_outcome():
    r = replay(synthetic_history(), Thresholds(), "runner", cooldown_hours=6, horizon_hours=24)
    assert r.fired and r.n_alerts == 1
    assert 1_000_000 <= r.mcap_at_alert <= 3_000_000
    assert r.peak_multiple > 1
    assert r.max_drawdown_pct <= 0
    assert r.ended_below_entry is not None


def test_replay_respects_volume_multiplier():
    r = replay(synthetic_history(), Thresholds(volume_multiplier=50, launch_window_hours=0), "runner", 6, 24)
    assert not r.fired
    # The synthetic token crosses $1M at ~8h old, so the early-launch path still catches it.
    r = replay(synthetic_history(), Thresholds(volume_multiplier=50), "runner", 6, 24)
    assert r.fired and r.trigger == "early_launch"


def test_unavailable_history_is_flagged():
    h = History("robinhood", "0xdead", error="token not found on GeckoTerminal")
    r = replay(h, Thresholds(), "runner", 6, 24)
    assert r.history.startswith("UNAVAILABLE")
    s = summarize([r])
    assert s["history_unavailable"] == 1 and s["runners"] == 0


def test_summary_counts_controls():
    runner = replay(synthetic_history(), Thresholds(), "runner", 6, 24)
    control = replay(synthetic_history(), Thresholds(volume_multiplier=50, launch_window_hours=0), "control", 6, 24)
    s = summarize([runner, control])
    assert s["hit_rate"] == 1.0 and s["false_positive_rate"] == 0.0


def test_parse_sweep():
    combos = parse_sweep(["volume_multiplier=2,3", "lookback_hours=2,3,6"])
    assert len(combos) == 6 and {"volume_multiplier": 2.0, "lookback_hours": 6.0} in combos


def test_load_inputs_csv_and_json(tmp_path):
    cfg = load_config(env={}, include_disabled=True)
    (tmp_path / "t.csv").write_text("chain,address,label\nBNB Chain,0xABC,runner\nrobinhood,0xdef,control\n")
    rows = load_inputs(str(tmp_path / "t.csv"), cfg)
    assert [(r.chain, r.address, r.label) for r in rows] == [("bsc", "0xabc", "runner"), ("robinhood", "0xdef", "control")]
    (tmp_path / "t.json").write_text('[{"chain": "solana", "address": "So1aNa"}]')
    rows = load_inputs(str(tmp_path / "t.json"), cfg, default_label="control")
    assert rows[0].address == "So1aNa" and rows[0].label == "control"


def test_config_env_overrides_and_per_chain_thresholds(tmp_path):
    (tmp_path / "c.toml").write_text(
        "[thresholds]\nvolume_multiplier = 4\n[chains.robinhood.thresholds]\nmin_liquidity_usd = 25000\n")
    cfg = load_config(str(tmp_path / "c.toml"), env={
        "SCANNER_CHAINS": "solana,robinhood",
        "SCANNER__SCANNER__POLL_INTERVAL_SECONDS": "60",
        "SCANNER__CHAINS__ROBINHOOD__THRESHOLDS__VOLUME_MULTIPLIER": "5",
    })
    assert set(cfg.chains) == {"solana", "robinhood"}
    assert cfg.poll_interval_seconds == 60
    assert cfg.chains["solana"].thresholds.volume_multiplier == 4
    assert cfg.chains["robinhood"].thresholds.volume_multiplier == 5
    assert cfg.chains["robinhood"].thresholds.min_liquidity_usd == 25000
    assert cfg.chains["solana"].thresholds.min_liquidity_usd == 50000


def test_pick_pool_prefers_launch_pool():
    from scanner.backtest import pick_pool
    pools = [
        {"attributes": {"address": "late", "reserve_in_usd": "5000000", "pool_created_at": "2026-09-08T00:00:00Z"}},
        {"attributes": {"address": "launch", "reserve_in_usd": "400000", "pool_created_at": "2026-07-13T00:00:00Z"}},
        {"attributes": {"address": "dust", "reserve_in_usd": "10", "pool_created_at": "2026-07-01T00:00:00Z"}},
    ]
    assert pick_pool(pools)["attributes"]["address"] == "launch"
    assert pick_pool(pools, "liquid")["attributes"]["address"] == "late"
