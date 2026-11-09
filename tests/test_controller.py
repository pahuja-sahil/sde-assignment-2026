import pytest

from controller import cohorts, config, db, stats
from controller.engine import _prompt_candidate_ok


def _lead(i, lang="hi", tier="1", age="60+", value="mid"):
    return (f"l{i}", "wf", "c1", f"p{i}", lang, tier, age, value, 0, "2027-01-04T00:00:00+05:30")


def test_stats_wilson_and_bh():
    lo, hi = stats.wilson(5, 10)
    assert 0.23 < lo < 0.25 and 0.75 < hi < 0.77
    passed = stats.benjamini_hochberg([0.01, 0.04, 0.03, 0.5], q=0.10)
    assert passed == [True, True, True, False]
    diff, z, p = stats.two_proportion_z(600, 1000, 500, 1000)
    assert diff == pytest.approx(0.1)
    assert p < 0.001
    assert stats.beta_prob_better(600, 1000, 500, 1000, "seed") > 0.99


def test_cohort_partition_is_disjoint_and_capped():
    conn = db.connect(":memory:")
    rows = []
    i = 0
    for age in cohorts.FEATURES["age_band"]:
        for lang in ("hi", "en"):
            for tier in ("1", "2", "3"):
                for _ in range(50):
                    rows.append(_lead(i, lang=lang, tier=tier, age=age))
                    i += 1
    conn.executemany("INSERT INTO leads (lead_id, workflow_id, campaign_id, person_id, lang, city_tier, "
                     "age_band, value_band, first_minute, first_ts) VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    ages = cohorts.age_cohorts(conn, "wf")
    assert len(ages) == 4
    part = cohorts.build_partition(conn, "wf", ages, [{"where": {"lang": ["hi"]}},
                                                       {"where": {"city_tier": ["1"]}}])
    assert 0 < len(part) <= 20
    assert len({c["id"] for c in part}) == len(part)
    sample = [{"lang": "hi", "city_tier": "1", "age_band": "60+", "value_band": "mid"},
              {"lang": "en", "city_tier": "3", "age_band": "18-30", "value_band": "low"}]
    for lead in sample:
        assert sum(1 for c in part if cohorts.matches(c["where"], lead)) == 1
    cohorts.save_cohorts(conn, "wf", part, 0, "test")
    assert len(cohorts.load_cohorts(conn, "wf")) == len(part)


def _wf():
    return {"options": {"welcome_message": ["W1", "W2"], "voice": ["V1", "V2"],
                        "caller_id": ["D1", "D2"], "prompt_version": ["P1", "P3"]},
            "dialling_window": [9, 21]}


def _base():
    return {"cohorts": [], "allocations": {"default": {
        "welcome_message": {"W1": 1.0}, "voice": {"V1": 1.0},
        "caller_id": {"D1": 0.5, "D2": 0.5}}}, "prompt_version": "P1",
        "schedule": {"hour_weights": {}, "lead_preferred_hour": {}}}


def test_config_compose_diff_shift_and_validate():
    base = _base()
    new = config.compose(base, cohorts=[{"id": "hi", "where": {"lang": ["hi"]}}],
                         allocations={"default": {"voice": {"V1": 0.8, "V2": 0.2}},
                                      "hi": {"welcome_message": {"W2": 1}}})
    assert new["allocations"]["default"]["voice"] == {"V1": pytest.approx(0.8), "V2": pytest.approx(0.2)}
    assert new["allocations"]["hi"]["welcome_message"] == {"W2": 1.0}
    assert config.changed_dims(base, new) == ["cohorts", "welcome_message", "voice"]
    voice_only = config.compose(base, allocations={"default": {"voice": {"V1": 0.8, "V2": 0.2}}})
    assert config.max_shift(base, voice_only) == pytest.approx(0.2)
    assert not config.validate(_wf(), new)
    assert config.impact_score(0.06, 0.5) == pytest.approx(0.3)
    assert config.diff(base, new)["changed_dims"] == ["cohorts", "welcome_message", "voice"]


def test_prompt_candidate_filter_keeps_grounded_and_drops_strict():
    grounded = ("Grounded: benefit claims restricted to policy-schedule fields; "
                "tighter objection handling")
    strict = "Strict: near-verbatim compliance script"
    options = ["P1", "P3", "P5"]
    assert _prompt_candidate_ok("P1", "P3", grounded, options)
    assert not _prompt_candidate_ok("P1", "P5", strict, options)
    assert not _prompt_candidate_ok("P1", "P1", "Current production prompt", options)
    assert not _prompt_candidate_ok("P1", "P9", "unknown", options)


def test_config_rejects_bad_hour_and_option():
    base = _base()
    bad = config.compose(base, prompt_version="P3")
    bad["schedule"]["hour_weights"] = {"default": {"7": 1}}
    problems = config.validate(_wf(), bad)
    assert any("outside window" in p for p in problems)
    bad2 = config.compose(base)
    bad2["allocations"]["default"]["voice"] = {"V9": 1}
    assert any("not allowed" in p for p in config.validate(_wf(), bad2))
