from core.ids import jd_hash, make_pk, normalize_label, normalize_text


def test_normalize_text_collapses_ws_and_edge_punct():
    assert normalize_text("Build  things.\n") == normalize_text("build things")


def test_normalize_label_matches_synonym_spacing():
    assert normalize_label("Notice period?") == normalize_label("notice  period")


def test_jd_hash_is_deterministic_under_cosmetic_edits():
    assert jd_hash("Build  things.\n") == jd_hash("build things.")


def test_make_pk_lowercases():
    assert make_pk("Acme", "123") == "acme#123"
