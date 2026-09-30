"""web_trail: understanding rich strings, judging hits, and spending search allowances."""
from mouseion import web_trail as W


def test_understand_series_volume_and_debris():
    assert W.understand("LNCS 3796") == {"title": "", "series": "Lecture Notes in Computer Science", "volume": "3796"}
    u = W.understand("Lecture Notes in Artif icial Intelligence 2831", {"artificial"})
    assert u["series"] == "Lecture Notes in Artificial Intelligence" and u["volume"] == "2831"
    assert W.understand("Equivalence relations and textbraceleft rm S textbraceright 5")["title"] == \
        "Equivalence relations and S 5"


def test_matches_needs_title_and_author_and_skips_shadow_libraries():
    entry = {"title": "Logic Is Not Occultism", "surnames": ["Kootte"]}
    good = W.Hit("Logic Is Not Occultism - A. Kootte - PhilPapers", "https://philpapers.org/rec/KOOLIN", "")
    assert W.matches(entry, good) >= 0.7
    assert W.matches(entry, W.Hit("Logic Is Not Occultism", "https://philpapers.org/rec/X", "by Smith")) == 0.0
    assert W.matches(entry, W.Hit("Logic Is Not Occultism Kootte", "https://libgen.rs/book/1", "")) == 0.0
    vol = {"title": "", "series": "Lecture Notes in Computer Science", "volume": "3796", "surnames": []}
    assert W.matches(vol, W.Hit("Cryptography and Coding | Lecture Notes in Computer Science vol 3796",
                                "https://link.springer.com/book/10.1007/11586821", "")) >= 0.8
    assert W.matches(vol, W.Hit("Lecture Notes in Computer Science | Springer", "https://link.springer.com/series/558", "")) == 0.0


class _Fake(W._Backend):
    def __init__(self, name, limit, hits=None, exc=None):
        self.name, self.limit, self.period = name, limit, "month"
        self.hits, self.exc, self.calls = hits or [], exc, 0

    def search(self, q):
        self.calls += 1
        if self.exc:
            raise self.exc
        return self.hits, 1


def test_pool_spends_allowances_evenly_and_stops(tmp_path):
    a = _Fake("a", 3, [W.Hit("t", "u", "")])
    b = _Fake("b", 2, [W.Hit("t", "u", "")])
    pool = W.SearchPool([a, b], tmp_path / "budget.json")
    for _ in range(5):
        pool.search("q")
    assert (a.calls, b.calls) == (3, 2)
    try:
        pool.search("q")
        raise AssertionError("should be exhausted")
    except W.Exhausted:
        pass
    again = W.SearchPool([_Fake("a", 3), _Fake("b", 2)], tmp_path / "budget.json")
    assert again.status() == {"a": 0, "b": 0}          # usage survives a restart


def test_pool_skips_exhausted_and_throttled(tmp_path):
    dead = _Fake("dead", 100, exc=W.Exhausted("quota"))
    slow = _Fake("slow", 100, exc=W.Throttled("429"))
    ok = _Fake("ok", 5, [W.Hit("t", "u", "")])
    pool = W.SearchPool([dead, slow, ok], tmp_path / "budget.json")
    assert pool.search("q")[0].url == "u"
    assert pool.remaining(dead) == 0 and "slow" in pool.resting


def test_pool_never_uses_metered_services_by_default(tmp_path):
    class Cfg:
        serper_api_key = "s"
        tavily_api_key = "t"
        brave_api_key = "b"
        gemini_search_api_key = "g"
    names = [b.name for b in W.SearchPool.from_config(Cfg(), tmp_path / "b.json").backends]
    assert names == ["serper", "tavily", "duckduckgo"]       # no brave, no gemini: they can bill
