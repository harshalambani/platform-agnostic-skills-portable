"""MAP-36: history pass -- unseen-with-account evidence, token spread discount,
single-token majority. Synthetic history only; no live book."""
from agents.skill_gnucash_account_mapper import agent as A

DRAW = "Equity:Drawings"
CCP = "Liabilities:Credit Card Payment"
BI = "Income:Bank Interest"


def score(desc, model):
    return A._history_bayes_score(set(A._tokenize_history(desc)), model)


def test_jvpg_club_and_club_mahindra_stay_drawings():
    model = {"jvpg": {DRAW: 5}, "club": {DRAW: 8}, "mahindra": {DRAW: 4}}
    assert score("JVPG Club", model)["account"] == DRAW
    assert score("Club Mahindra", model)["account"] == DRAW


def test_credit_interest_stays_bank_interest():
    model = {"credit": {BI: 6, CCP: 1}, "interest": {BI: 9}}
    assert score("CREDIT INTEREST", model)["account"] == BI


def test_unseen_token_counts_against_the_account():
    # "cred" is 48:1 CC payment; "club" is 8 rows of Drawings. The old model
    # let the Drawings account win on "club" alone; it must not be READY.
    model = {"cred": {CCP: 48, "Expenses:Misc": 1}, "club": {DRAW: 8}}
    got = score("CRED CLUB", model)
    assert got is None or got["account"] != DRAW


def test_smoothing_lowers_a_score_when_a_token_is_missing():
    model = {"alpha": {DRAW: 5}, "beta": {CCP: 5}}
    both = dict((a, s) for s, a in A._history_bayes_raw({"alpha", "beta"}, model)[0])
    alone = dict((a, s) for s, a in A._history_bayes_raw({"alpha"}, model)[0])
    # the account that lacks "beta" is scored lower once "beta" is in the row
    assert both[DRAW] < alone[DRAW]


def test_wide_spread_token_carries_little():
    many = {f"Expenses:E{i}": 1 for i in range(8)}
    many[DRAW] = 3
    model = {"bangalore": many, "zorbax": {DRAW: 3}}
    raw = A._history_bayes_raw({"bangalore", "zorbax"}, model)
    only_spread = A._history_bayes_raw({"bangalore"}, model)
    assert raw[0][0][1] == DRAW
    # on its own the common word is far from decisive
    assert only_spread is None or only_spread[0][0][0] < 0.9


def test_generic_single_word_never_ready_alone():
    many = {f"Expenses:E{i}": 1 for i in range(6)}
    many[DRAW] = 20
    model = {"urban": many}
    assert score("Urban", model) is None


def test_single_token_needs_strong_majority():
    # 60/40 plurality on a single token is not READY
    model = {"acme": {DRAW: 6, CCP: 4}}
    assert score("ACME", model) is None
    # 9:1 is
    model = {"acme": {DRAW: 38, CCP: 2}}
    assert score("ACME", model)["account"] == DRAW


def test_no_new_win_where_margin_gate_refused():
    model = {"foo": {DRAW: 5, CCP: 5}}
    assert score("foo", model) is None
