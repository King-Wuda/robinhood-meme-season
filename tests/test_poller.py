from scanner.config import load_config
from scanner.db import Store
from scanner.models import Candidate, PairData
from scanner.poller import Poller
from scanner.trigger import HOUR

NOW = 1_800_000_000.0


class FakeTelegram:
    enabled = True

    def __init__(self):
        self.sent = []

    def send(self, text):
        self.sent.append(text)
        return True


def pair(mc, h1=60_000, h6=120_000, h24=200_000, pc_h1=None, liq=80_000, token="0xabc"):
    return PairData("robinhood", token, "0xpair", "Test", "TST", 0.001, mc, "marketCap", mc, liq,
                    h1, h6, h24, pc_h1, NOW - 10 * HOUR, "", "dexscreener")


class FakePoller(Poller):
    def __init__(self, cfg, store, tg):
        super().__init__(cfg, store, api=None, telegram=tg, clock=lambda: self.now)
        self.now = NOW
        self.data = {}

    def fetch(self, chain, addresses):
        return {a: self.data[a] for a in addresses if a in self.data}


def make(tmp_path):
    cfg = load_config(env={"SCANNER_CHAINS": "robinhood"})
    store = Store(str(tmp_path / "t.db"))
    tg = FakeTelegram()
    return FakePoller(cfg, store, tg), store, tg


def test_alert_from_snapshot_history_then_cooldown(tmp_path):
    p, store, tg = make(tmp_path)
    chain = p.cfg.chains["robinhood"]
    cand = {"0xabc": Candidate("robinhood", "0xabc", "test")}

    p.data["0xabc"] = pair(700_000, h1=10_000)
    p.process_chain(chain, cand, NOW)
    assert store.is_tracked("robinhood", "0xabc") and not tg.sent

    p.data["0xabc"] = pair(2_500_000)
    assert p.process_chain(chain, {}, NOW + 1800) == 1
    assert "Robinhood Chain" in tg.sent[0] and "3.0x" in tg.sent[0] and "0xabc" in tg.sent[0]
    assert tg.sent[0].startswith("🚀🆕 Momentum + early launch")

    # Still firing 1h later, but inside the 6h cooldown.
    assert p.process_chain(chain, {}, NOW + 3600) == 0
    assert len(tg.sent) == 1


def test_price_change_inference_detects_crossover_on_first_sight(tmp_path):
    p, store, tg = make(tmp_path)
    chain = p.cfg.chains["robinhood"]
    p.data["0xabc"] = pair(2_000_000, pc_h1=150)  # was $800k an hour ago
    assert p.process_chain(chain, {"0xabc": Candidate("robinhood", "0xabc", "test")}, NOW) == 1


def test_rejects_below_discovery_floor_and_drops_stale(tmp_path):
    p, store, tg = make(tmp_path)
    chain = p.cfg.chains["robinhood"]
    p.data["0xdust"] = pair(5_000, token="0xdust")
    p.process_chain(chain, {"0xdust": Candidate("robinhood", "0xdust", "test")}, NOW)
    assert not store.is_tracked("robinhood", "0xdust")

    p.data["0xabc"] = pair(500_000)
    p.process_chain(chain, {"0xabc": Candidate("robinhood", "0xabc", "test")}, NOW)
    p.data["0xabc"] = pair(500_000, h1=0, h6=0, h24=0)
    p.process_chain(chain, {}, NOW + 2 * HOUR)
    assert not store.is_tracked("robinhood", "0xabc")
