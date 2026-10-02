from scanner.trigger import HOUR, Observation, Thresholds, evaluate, find_crossover, in_cooldown, volume_multiple

NOW = 1_800_000_000.0
TH = Thresholds()


def obs(mc=2_000_000, h1=60_000, h6=120_000, liq=80_000, age_h=10):
    return Observation(NOW, mc, h1, h6, liq, NOW - age_h * HOUR)


def crossed_history():
    return [(NOW - 2 * HOUR, 600_000), (NOW - HOUR, 900_000), (NOW - 0.5 * HOUR, 1_400_000)]


def test_fires_when_all_conditions_hold():
    res = evaluate(obs(age_h=30), crossed_history(), TH)
    assert res.fired, res.failed
    assert res.reasons == ["volume_spike"]
    assert res.volume_multiple == 3.0  # 60k / (120k / 6)
    assert res.crossed_after == NOW - HOUR
    assert res.crossed_at == NOW - 0.5 * HOUR


def test_band_limits():
    assert "market_cap_band" in evaluate(obs(mc=900_000), crossed_history(), TH).failed
    assert "market_cap_band" in evaluate(obs(mc=11_000_000), crossed_history(), TH).failed


def test_no_crossover_if_below_point_is_outside_lookback():
    history = [(NOW - 4 * HOUR, 500_000), (NOW - 2 * HOUR, 1_500_000)]
    assert "crossover" in evaluate(obs(), history, TH).failed


def test_no_crossover_if_always_in_band():
    history = [(NOW - h * HOUR, 3_000_000) for h in (0.5, 1, 2)]
    assert "crossover" in evaluate(obs(), history, TH).failed


def test_crossing_time_is_after_last_below_point():
    # Dipped below again 20 min ago: the recent crossing counts, not the earlier one.
    history = [(NOW - 2 * HOUR, 800_000), (NOW - HOUR, 1_500_000), (NOW - 1200, 950_000)]
    assert find_crossover(NOW, 1_000_000, 3, history) == (NOW - 1200, NOW)


def test_volume_spike_threshold():
    assert "volume_spike" in evaluate(obs(h1=50_000, age_h=30), crossed_history(), TH).failed
    assert "volume_spike" in evaluate(obs(h1=50_000), crossed_history(), Thresholds(launch_window_hours=0)).failed


def test_early_launch_fires_without_volume_spike():
    # 10h-old pair, crossed $1M 1h ago, flat volume (1.0x): early-launch path.
    res = evaluate(obs(h1=20_000, age_h=10), crossed_history(), TH)
    assert res.fired and res.reasons == ["early_launch"]


def test_early_launch_window_is_measured_from_launch():
    # Same crossing, but the pair is 30h old: it was over a day old while still under $1M.
    res = evaluate(obs(h1=20_000, age_h=30), crossed_history(), TH)
    assert not res.fired and "volume_spike" in res.failed
    # Crossing at 23h old still counts even if evaluated after the 24h mark.
    res = evaluate(obs(h1=20_000, age_h=24.5), crossed_history(), TH)
    assert res.fired


def test_early_launch_optional_volume_floor():
    th = Thresholds(launch_min_volume_to_mcap=0.1)  # needs 1h vol >= $200k on a $2M cap
    assert not evaluate(obs(h1=20_000, age_h=10), crossed_history(), th).fired
    assert evaluate(obs(h1=250_000, h6=2_000_000, age_h=10), crossed_history(), th).reasons == ["early_launch"]


def test_both_paths_reported():
    assert evaluate(obs(age_h=10), crossed_history(), TH).reasons == ["volume_spike", "early_launch"]


def test_young_pair_volume_baseline_uses_its_age():
    # 2h-old pair: 6h window only has 2h of volume, so the hourly avg is h6/2.
    assert volume_multiple(90_000, 100_000, 2) == 1.8
    # Under 1h old: no history of its own, multiple is 1.
    assert volume_multiple(50_000, 50_000, 0.5) == 1.0
    assert volume_multiple(10, 0, 3) is None


def test_sanity_filters():
    assert "liquidity" in evaluate(obs(liq=40_000), crossed_history(), TH).failed
    assert "liquidity" in evaluate(obs(liq=None), crossed_history(), TH).failed
    assert "liquidity" not in evaluate(obs(liq=None), crossed_history(), TH, require_liquidity=False).failed
    assert "pair_age" in evaluate(obs(age_h=0.2), crossed_history(), TH).failed


def test_cooldown():
    assert in_cooldown(NOW - 5 * HOUR, NOW, 6)
    assert not in_cooldown(NOW - 7 * HOUR, NOW, 6)
    assert not in_cooldown(None, NOW, 6)
