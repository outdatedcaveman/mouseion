"""grounded_meta helpers + the DOI-collision record in db.replace_ref."""
import sys
from pathlib import Path

import pytest

from mouseion.db import RefDatabase
from mouseion.models import Author, Reference

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


@pytest.fixture(scope="module")
def G():
    argv = sys.argv
    sys.argv = ["grounded_meta.py", "lx", "0", "dry"]
    sys.path.insert(0, str(SCRIPTS))
    try:
        import grounded_meta
        yield grounded_meta
    finally:
        sys.argv = argv


def test_spacing_accents_fold_onto_their_letter(G):
    assert G._fix_accents("J¨orn Guy S¨uß") == "Jörn Guy Süß"
    assert G._fix_accents("F´ısica") == "Física"          # TeX's dotless i
    assert G._fix_accents("plain title") == "plain title"


class _C:
    def __init__(self, title):
        self.title = title


def test_crossref_title_with_markup_or_dropped_math_does_not_win(G):
    c = _C("SOME CONSEQUENCES OF AND")
    G._keep_better_title("Some consequences of mathrmTD and mathrmsTD", c)
    assert c.title == "Some consequences of mathrmTD and mathrmsTD"
    c = _C("Playing Pool with <mml:math><mml:mi>x</mml:mi></mml:math>: from Bouncing Billiards")
    G._keep_better_title("Playing Pool with x: from Bouncing Billiards", c)
    assert "<" not in c.title


def test_record_completing_a_truncated_title_wins(G):
    c = _C("Deformations of quasi-categories in modules")
    G._keep_better_title("Deformations of quasi", c)
    assert c.title == "Deformations of quasi-categories in modules"


def test_colliding_doi_is_recorded_not_silently_dropped(tmp_path):
    db = RefDatabase(path=tmp_path / "t.db")
    db.open()
    a = db.upsert(Reference(title="Fluctuations-Induced Quantum Radiation", doi="10.3390/physics5020040",
                            authors=[Author(family="Bravo", given="M.")], year=2023))
    b = db.upsert(Reference(title="Fluctuations-Induced Quantum Radiation (preprint)", arxiv_id="2303.06831",
                            authors=[Author(family="Bravo", given="M.")], year=2023))
    ref = db.get(b)
    ref.doi = "10.3390/physics5020040"
    db.replace_ref(b, ref)
    assert db.get(b).doi in (None, "")                      # unique DOI still respected
    with db._db() as conn:
        rows = conn.execute("SELECT ref_id, doi, holder_id FROM doi_collisions").fetchall()
    assert [tuple(r) for r in rows] == [(b, "10.3390/physics5020040", a)]
    db.close()


def test_title_word_authors_cannot_veto(G):
    seed = Reference(title="A Problem Solving Approa", authors=[Author(family="Maple"), Author(family="Mathematica")])
    cand = Reference(title="Maple and Mathematica: A Problem Solving Approach for Mathematics",
                     authors=[Author(family="Shingareva", given="Inna")])
    assert G.P._authors_are_title_words(seed, cand)
    real = Reference(title="x", authors=[Author(family="Maple", given="J.")])
    assert not G.P._authors_are_title_words(real, cand)            # a given name: a real author
