"""PDF ingest: identifiers hidden in archive file names, book file names, relevance."""
from mouseion import pdf_ingest as PI


def test_filename_dois():
    assert PI.filename_dois("PAMS/Volume 003/1952_v003_n02/2032262.pdf") == ["10.2307/2032262"]
    assert PI.filename_dois("PRL/1997-2003/v84/i23/PRL_v84_i23_p5331_1.pdf") == ["10.1103/PhysRevLett.84.5331"]
    assert PI.filename_dois("PhysRevLett 2004-2006/pdf/PRL/v95/i25/e257003.pdf") == ["10.1103/PhysRevLett.95.257003"]
    assert "10.1088/1751-8113/43/7/075303" in PI.filename_dois("2010 Volume 43/1751-8121_43_7_075303.pdf")
    assert PI.filename_dois("Fundamenta/117/On the netweight of subspaces.pdf") == []


def test_elsevier_pii_and_doi_in_file_names():
    f = PI.PdfFacts(path="x/1-s2.0-S0168007200000580-main.pdf")
    stem = "1-s2.0-S0168007200000580-main"
    m = PI.PII_RE.search(stem.replace("1-s2.0-", "-"))
    s_, a, b, yy, item, chk = m.groups()
    assert f"10.1016/{s_}{a}-{b}({yy}){item}-{chk}" == "10.1016/S0168-0072(00)00058-0"
    m = PI.FILE_DOI_RE.search("10.1007_s00220-019-03608-z")
    assert m.group(1) + "/" + m.group(2) == "10.1007/s00220-019-03608-z"


def test_book_file_names():
    assert PI._split_title_author("Probability, A. N. Shiryaev") == ("Probability", "A. N. Shiryaev")
    assert PI._split_title_author("[David_Gelernter]_Mirror_Worlds") == ("Mirror Worlds", "David Gelernter")


def test_relevance_skips_manuals_and_basic_textbooks_not_papers():
    keep, _ = PI.relevance(PI.PdfFacts(path="Books/Owner's Manual - Printer X200.pdf", text="x" * 300))
    assert not keep
    keep, _ = PI.relevance(PI.PdfFacts(path="Books/Apostila de Língua Portuguesa 5 ano.pdf", text="x" * 300))
    assert not keep
    keep, _ = PI.relevance(PI.PdfFacts(path="Papers/On manuals.pdf", text="user manual design", doi="10.1/x"))
    assert keep
    keep, _ = PI.relevance(PI.PdfFacts(path="Videos/Courses/lecture.pdf", text="x" * 300))
    assert not keep


def test_course_material_brazilian_only():
    cm = PI.course_material
    for t in ["Equacoes Diferenciais C Lista de Exercicios 5", "CRP0420 - Slides Aula 6",
              "Questionário - FLF5045", "Relatório de Atividades do Facilitador UNIVESP - AGO/21",
              "Trabalho Final de Preparação Pedagógica (EAH5001)", "Apuração 3ª Chamada - Vestibular Univesp 2020",
              "Nº USP DISCIPLINA DATA DA PROVA OBSERVAÇÃO EAC0510", "Radiacao Termica e o Postulado de Planck (ementa do curso"]:
        assert cm(t), t
    for t in ["Tra il dire e il fare. Gli esperti morali alla prova", "Analisti del linguaggio in cerca di una disciplina",
              "A ONTOLOGIA É FUNDAMENTAL? O primado da ontologia entre as disciplinas do conhecimento",
              "Towards homotopy canonicity for propositional type theory || slides_van_den_berg",
              "Dissertação apresentada ao Programa de Pós-Graduação; lista de exercícios em anexo",
              "O uso de provas na sala de aula", "Proceedings of CSL 2022 lecture slides"]:
        assert not cm(t), t
    keep, why = PI.relevance(PI.PdfFacts(path="Papers/CRP0420 - Slides Aula 6.pdf", text="x" * 300))
    assert not keep and why.startswith("course material")


def test_archive_course_material_is_recoverable(tmp_path):
    import sqlite3
    from mouseion.db import RefDatabase
    from mouseion.models import Author, Reference
    path = tmp_path / "refs.db"
    with RefDatabase(path=path) as db:
        lista = db.upsert(Reference(title="Equacoes Diferenciais C Lista de Exercicios 5",
                                    authors=[Author(family="Prof", given="X")]), tags=["archive:archives"])
        paper = db.upsert(Reference(title="Tra il dire e il fare. Gli esperti morali alla prova",
                                    authors=[Author(family="Croce", given="M")]))
        published = db.upsert(Reference(title="Lista de exercícios comentada", doi="10.1590/x",
                                        authors=[Author(family="Silva", given="A")]))
    c = sqlite3.connect(path, isolation_level=None)
    gone = PI.archive_course_material(c, True)
    assert [g[0] for g in gone] == [lista]
    ids = {r[0] for r in c.execute("SELECT id FROM refs")}
    assert lista not in ids and {paper, published} <= ids
    assert c.execute("SELECT archive_rule FROM refs_duplicates WHERE id=?", (lista,)).fetchone() == ("course-material",)
    assert c.execute("SELECT tag FROM refs_removed_tags WHERE ref_id=?", (lista,)).fetchall() == [("archive:archives",)]


def test_authors_from_page_reads_the_author_line():
    F = PI.PdfFacts
    cases = [
        ("POTENTIAL USES OF REPRESENTATIONS OF SL(4, R) IN\nPARTICLE PHYSICS\nROBERT ARNOTT WILSON\nAbstract. I",
         "POTENTIAL USES OF REPRESENTATIONS OF SL(4, R) IN PARTICLE PHYSICS", [("Robert Arnott", "Wilson")]),
        ("ASYMPTOTIC\nANALYSIS\nOF DAUBECHIES\nPOLYNOMIALS\nJIANHONG SHEN AND GILBERT STRANG\n(Communicated by X)",
         "ASYMPTOTIC ANALYSIS OF DAUBECHIES POLYNOMIALS", [("Jianhong", "Shen"), ("Gilbert", "Strang")]),
        ("JAMES D. McCAWLEY\nNATURAL DEDUCTION AND ORDINARY\nLANGUAGE DISCOURSE STRUCTURE\nText",
         "NATURAL DEDUCTION AND ORDINARY LANGUAGE DISCOURSE STRUCTURE", [("James D.", "McCawley")]),
        ("GERT H. MULLER\nREFLECTION IN SET THEORY\nTHE BERNAYS-LEVY AXIOM SYSTEM\nIntroduction 1",
         "REFLECTION IN SET THEORY THE BERNAYS-LEVY AXIOM SYSTEM", [("Gert H.", "Muller")]),
        ("What is Logical in First-Order Logic?\nBoris Čulina\nDepartment of Mathematics",
         "What is Logical in First-Order Logic?", [("Boris", "Čulina")]),
        ("Agenda de Privatizacoes\nAvancos e Desafios\nBrasilia 2019", "Agenda de Privatizacoes", []),
    ]
    for text, title, want in cases:
        got = [(a.given, a.family) for a in PI.authors_from_page(F(path="x.pdf", text=text, font_title=title))]
        assert got == want, (title, got)


def test_ams_pii_and_filename_author():
    m = PI.AMS_PII_RE.search("Volume 129, Pages 1825-1831 S 0002-9939(00)05766-X Article")
    assert m and m.groups() == ("0002-9939", "00", "05766", "X")
    assert PI.filename_author("Gao_2022_Why_quantum").family == "Gao"
    assert PI.filename_author("da_Costa_2006_Logic").family == "da Costa"
    assert PI.filename_author("D_2008_Elliptic") is None
    assert PI.filename_author("Unknown_2001_Something") is None


def test_name_vocab_rejects_phrases(tmp_path):
    import sqlite3
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE refs (authors TEXT, title TEXT, abstract TEXT)")
    abstract = ("We study artificial intelligence and its forms. Following Hume, the argument of Hume "
                "and of Gödel shows that intelligence takes many forms; intelligence, forms, and "
                "epistemology recur in epistemology and in forms of intelligence.")
    c.executemany("INSERT INTO refs VALUES (?,?,?)", [("[]", "t", abstract)] * 3)
    v = PI.NameVocab(c, min_count=3)
    from mouseion.models import Author
    assert not v.ok([Author(family="Intelligence", given="Artificial")])
    assert not v.ok([Author(family="Forms", given="Differential")])
    assert v.ok([Author(family="Hume", given="David")])


def test_fill_plan_fills_only_empty_fields():
    from mouseion.models import Author, Reference, RefType
    seed = Reference(title="M11p T x11 lp A I", year=2001)
    rec = Reference(title="Bounded point evaluations for cyclic operators", year=1999, doi="10.1090/x",
                    journal="Proc. AMS", authors=[Author(family="Bourhim", given="A.")])
    up = PI.fill_plan(seed, rec, pages=10)
    assert up["title"].startswith("Bounded") and up["doi"] == "10.1090/x" and "authors_json" in up
    assert "year" not in up                    # a stored year is kept
    chapter = Reference(title="Objective Lenses", ref_type=RefType.BOOK_CHAPTER, authors=[Author(family="Keller")])
    assert PI.fill_plan(Reference(title="Handbook of Confocal Microscopy"), chapter, pages=900) == {}


def test_junk_review_fix_note_parsing():
    import importlib.util, json, sys
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    sys.argv = ["apply_junk_decisions.py", "x", "dry"]
    spec = importlib.util.spec_from_file_location("ajd", root / "scripts" / "apply_junk_decisions.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    up = m.parse_note("The Lazy Lambda Calculus / Samson Abramsky / 1990")
    assert up["title"] == "The Lazy Lambda Calculus" and up["year"] == 1990
    assert json.loads(up["authors_json"])[0]["family"] == "Abramsky"
    assert m.parse_note("Just a title") == {"title": "Just a title"}
