from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "polymarket_news_radar.py"
FIXTURE_PATH = ROOT / "tests" / "fixtures" / "polymarket_news_radar_fixture_20260617.json"
FIXTURE_20260618_PATH = ROOT / "tests" / "fixtures" / "polymarket_news_radar_fixture_20260618.json"


_STATE_REDIRECT: Path | None = None


@pytest.fixture(autouse=True)
def _keep_production_state_untouched(tmp_path):
    """Send every module-level state path into the test's own tmp dir.

    The radar resolves its state paths once at import time, so a test that
    drives run() writes the real state/ directory unless each constant is
    rebound. Rebinding them by scanning — instead of listing them one by one —
    also covers state files added by later versions: PROBE_SNAPSHOT_PATH
    arrived in V3.4, nobody rebound it, and a plain `pytest` run overwrote the
    probability baseline of a live deployment.
    """
    global _STATE_REDIRECT
    _STATE_REDIRECT = tmp_path / "radar-state"
    try:
        yield
    finally:
        _STATE_REDIRECT = None


def load_module():
    spec = importlib.util.spec_from_file_location("polymarket_news_radar", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["polymarket_news_radar"] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    _redirect_writable_paths(module)
    return module


def _redirect_writable_paths(module) -> None:
    if _STATE_REDIRECT is None:  # module loaded outside a test
        return
    sandbox = _STATE_REDIRECT
    sandbox.mkdir(parents=True, exist_ok=True)
    for name, value in list(vars(module).items()):
        if isinstance(value, Path) and value.parent in (module.STATE_DIR, module.OUTBOX_DIR):
            setattr(module, name, sandbox / value.name)
    module.STATE_DIR = sandbox
    module.OUTBOX_DIR = sandbox / "outbox"
    # Tests must never reach a deployment's real credentials, not even to read.
    module.ENV_PATH = sandbox / ".env"


def load_fixture():
    return json.loads(FIXTURE_PATH.read_text())


def load_fixture_20260618():
    return json.loads(FIXTURE_20260618_PATH.read_text())


def build_fixture_signals(radar, fixture):
    now = datetime.fromisoformat(fixture["now_utc"])
    signals = []
    for sample in fixture["samples"]:
        event = sample["event"]
        market = event["markets"][0]
        signal, rejected = radar.build_signal(event, market, "confirmed", now)
        assert rejected is None
        assert signal is not None
        signals.append(signal)
    return sorted(signals, key=lambda item: item.score, reverse=True)


def synthetic_signal(radar, category, section_key: str, index: int, score: float):
    labels = ("alpha", "bravo", "charlie", "delta")
    label = labels[index]
    return radar.Signal(
        event_id=f"event-{section_key}-{label}",
        market_id=f"market-{section_key}-{label}",
        category=category,
        title=f"Synthetic {section_key} {label} story",
        market_title=f"Synthetic {section_key} {label} market",
        outcome="Yes",
        outcome_prices=(("Yes", 0.7), ("No", 0.3)),
        probability=0.7,
        delta_1h=None,
        delta_24h=0.12,
        delta_1w=None,
        volume_24h=100000 + index,
        liquidity=50000,
        end_date="2026-07-31",
        effective_deadline_utc="2026-07-31T00:00:00+00:00",
        effective_deadline_sgt="2026-07-31 08:00 SGT",
        deadline_source="test",
        ttl_hours=720,
        eligibility_status="eligible",
        reject_reason=None,
        source_status="confirmed",
        category_reason="test",
        translation_status="ok",
        score_breakdown={"test": score},
        event_url="",
        score=score,
        reason="test",
    )


def synthetic_event(question: str, title: str | None = None):
    market = {
        "id": question.lower().replace(" ", "-")[:40],
        "question": question,
        "active": True,
        "closed": False,
        "acceptingOrders": True,
        "outcomes": "[\"Yes\", \"No\"]",
        "outcomePrices": "[\"0.70\", \"0.30\"]",
        "volume24hr": 150000,
        "liquidity": 50000,
        "oneDayPriceChange": 0.08,
        "endDateIso": "2026-07-31",
    }
    return {
        "id": f"event-{market['id']}",
        "title": title or question,
        "slug": market["id"],
        "active": True,
        "closed": False,
        "markets": [market],
    }


def test_effective_deadline_prefers_market_deadline():
    radar = load_module()
    fixture = load_fixture()
    sample = next(item for item in fixture["samples"] if item["name"] == "expired_iran_peace_deal")
    event = sample["event"]
    market = event["markets"][0]

    deadline, source = radar.effective_deadline(event, market)

    assert source == "market.endDate"
    assert radar.sgt_datetime_label(deadline) == "2026-06-15 08:00 SGT"


def test_expired_market_is_rejected():
    radar = load_module()
    fixture = load_fixture()
    now = datetime.fromisoformat(fixture["now_utc"])
    sample = next(item for item in fixture["samples"] if item["name"] == "expired_iran_peace_deal")
    event = sample["event"]
    market = event["markets"][0]

    signal, rejected = radar.build_signal(event, market, "events_only", now)

    assert signal is None
    assert rejected is not None
    assert rejected["reject_reason"] == "deadline_passed"
    assert rejected["effective_deadline_sgt"] == "2026-06-15 08:00 SGT"


def test_title_templates_and_category_routing():
    radar = load_module()
    fixture = load_fixture()
    now = datetime.fromisoformat(fixture["now_utc"])

    expectations = {
        "future_iran_text_release": ("geopolitics", "美国-伊朗协议文本是否在6月19日前发布"),
        "fed_no_cuts_2026": ("macro_finance", "2026年是否没有美联储降息"),
        "fed_july_no_change": ("macro_finance", "2026年7月会议后美联储利率是否不变"),
        "dota_dragon_market": ("sports", "第1局双方是否都击杀小龙"),
        "crude_low_75": ("macro_finance", "原油在6月底是否触及75美元（低点）"),
    }

    for sample in fixture["samples"]:
        if sample["name"] not in expectations:
            continue
        expected_category, expected_title = expectations[sample["name"]]
        event = sample["event"]
        market = event["markets"][0]
        signal, rejected = radar.build_signal(event, market, "confirmed", now)
        assert rejected is None
        assert signal is not None
        assert signal.category.key == expected_category
        assert radar.market_title_cn(signal) == expected_title
        assert signal.translation_status == "ok"


def test_section_items_excludes_low_quality_and_expired():
    radar = load_module()
    fixture = load_fixture()
    now = datetime.fromisoformat(fixture["now_utc"])
    signals = []
    for sample in fixture["samples"]:
        event = sample["event"]
        market = event["markets"][0]
        signal, _rejected = radar.build_signal(event, market, "confirmed", now)
        if signal:
            signals.append(signal)
    signals = sorted(signals, key=lambda item: item.score, reverse=True)

    geopolitics = radar.section_items(signals, ("geopolitics",))
    macro = radar.section_items(signals, ("macro_finance",))

    assert all("June 15" not in signal.market_title for signal in geopolitics)
    assert any("Crude Oil" in signal.market_title for signal in macro)


def test_signal_to_dict_contains_diagnostics_schema():
    radar = load_module()
    fixture = load_fixture()
    now = datetime.fromisoformat(fixture["now_utc"])
    sample = next(item for item in fixture["samples"] if item["name"] == "crude_low_75")
    event = sample["event"]
    market = event["markets"][0]
    signal, rejected = radar.build_signal(event, market, "confirmed", now)

    assert rejected is None
    assert signal is not None
    payload = radar.signal_to_dict(signal)
    for key in (
        "effective_deadline_utc",
        "effective_deadline_sgt",
        "ttl_hours",
        "eligibility_status",
        "source_status",
        "category_reason",
        "translation_status",
        "score_breakdown",
    ):
        assert key in payload


def test_section_report_items_groups_related_markets():
    radar = load_module()
    signals = build_fixture_signals(radar, load_fixture_20260618())

    geopolitics = radar.section_report_items(signals, ("geopolitics",))
    story_keys = [item.story_key for item in geopolitics]

    assert len(story_keys) == len(set(story_keys))
    meeting_item = next(item for item in geopolitics if item.story_key.endswith(":us_iran_diplomatic_meeting"))
    assert len(meeting_item.signals) == 3
    rendered = radar.report_line(meeting_item, 1, "🟦")
    assert "美国与伊朗外交会谈" in rendered
    assert "6月19日前" in rendered
    assert "6月21日前" in rendered
    assert "瑞士举行" in rendered


def test_tech_section_groups_musk_ranges_and_keeps_claude_out_of_sports():
    radar = load_module()
    signals = build_fixture_signals(radar, load_fixture_20260618())

    tech = radar.section_report_items(signals, ("tech_business",))
    sports = radar.section_report_items(signals, ("sports", "entertainment_culture"))

    musk_item = next(item for item in tech if item.story_key.endswith(":elon_musk_tweet_count"))
    assert len(musk_item.signals) == 3
    assert "160-179" in radar.report_line(musk_item, 1, "🟪")
    claude_signal = next(signal for signal in signals if "Claude Fable" in signal.market_title)
    assert claude_signal.category.key == "tech_business"
    assert all("Claude" not in signal.market_title for item in sports for signal in item.signals)


def test_sports_match_title_is_fully_chinese():
    radar = load_module()
    signals = build_fixture_signals(radar, load_fixture_20260618())
    signal = next(signal for signal in signals if signal.market_id == "usa-win-june19")

    assert signal.category.key == "sports"
    assert radar.market_title_cn(signal) == "美国是否在2026年6月19日获胜"
    assert "是否美国 赢得" not in radar.market_title_cn(signal)


def test_needs_review_title_is_downgraded_not_rejected():
    radar = load_module()
    now = datetime(2026, 6, 17, 7, 0, tzinfo=timezone.utc)
    event = {
        "id": "nfl-event",
        "title": "NFL Champion 2027",
        "slug": "nfl-champion-2027",
        "active": True,
        "closed": False,
        "markets": [
            {
                "id": "nfl-bengals",
                "question": "Will the Cincinnati Bengals win the 2027 NFL league championship?",
                "active": True,
                "closed": False,
                "acceptingOrders": True,
                "outcomes": "[\"Yes\", \"No\"]",
                "outcomePrices": "[\"0.21\", \"0.79\"]",
                "volume24hr": 320000,
                "liquidity": 45000,
                "oneDayPriceChange": 0.12,
                "endDateIso": "2027-03-31",
            }
        ],
    }

    signal, rejected = radar.build_signal(event, event["markets"][0], "events_search", now)

    assert rejected is None
    assert signal is not None
    assert signal.translation_status in {"ok", "needs_review"}
    assert radar.market_title_cn(signal) == "辛辛那提猛虎是否赢得2027年NFL总冠军"


def test_render_text_message_omits_empty_placeholders():
    radar = load_module()
    signals = build_fixture_signals(radar, load_fixture_20260618())

    rendered = radar.render_text_message(signals, total_count=999, eligible_count=len(signals))

    assert "暂无新增强信号" not in rendered
    assert "展示 <code>" in rendered
    assert "<blockquote expandable>" in rendered
    assert len(rendered) <= 4096
    assert rendered.count("<blockquote") == rendered.count("</blockquote>")


def test_render_caption_is_short_digest_entrypoint():
    radar = load_module()
    signals = build_fixture_signals(radar, load_fixture_20260618())
    sections = radar.report_items_by_section(signals)

    rendered = radar.render_caption(
        signals,
        total_count=999,
        eligible_count=len(signals),
        display_sections=sections,
    )

    assert "今天先看：" in rendered
    assert "马斯克" not in rendered
    assert "六大板块明细见下一条" not in rendered
    assert "Polymarket" in rendered
    assert radar.visible_html_len(rendered) <= radar.DIGEST_CAPTION_MAX_CHARS


def test_deliver_digest_sends_exactly_one_photo_message(monkeypatch, tmp_path):
    radar = load_module()
    image_path = tmp_path / "radar.png"
    image_path.write_bytes(b"png")
    calls = []

    def fake_send_photo(token, chat_id, thread_id, path, caption):
        calls.append(("photo", token, chat_id, thread_id, path, caption))
        return {"result": {"message_id": 42}}

    def fail_send_message(*_args, **_kwargs):
        raise AssertionError("daily digest must not send a second text message")

    monkeypatch.setattr(radar, "send_photo", fake_send_photo)
    monkeypatch.setattr(radar, "send_message", fail_send_message)

    delivery = radar.deliver_digest("token", "-100", 412, "摘要", image_path)

    assert len(calls) == 1
    assert delivery == {"method": "sendPhoto", "photo_message_id": 42}


def test_digest_caption_uses_exact_count_and_never_truncates_titles():
    radar = load_module()
    categories = {category.key: category for category in radar.CATEGORIES}
    long_title = "美国与伊朗是否会在下一轮外交会谈前公布包含能源制裁核查安排执行时间表国际监督机制及争端解决条款的完整协议文本"
    assert 48 < len(long_title) <= 64
    geo = replace(
        synthetic_signal(radar, categories["geopolitics"], "geopolitics", 0, 90.0),
        title=long_title,
        market_title=long_title,
        volume_24h=2_000_000,
    )
    macro_title = "原油在7月是否触及100美元"
    macro = replace(
        synthetic_signal(radar, categories["macro_finance"], "macro_finance", 1, 88.0),
        title=macro_title,
        market_title=macro_title,
        volume_24h=1_500_000,
    )
    sections = {
        "geopolitics": [
            radar.ReportItem(radar.story_key(geo), long_title, (geo,), geo.score, geo.volume_24h)
        ],
        "macro_finance": [
            radar.ReportItem(radar.story_key(macro), macro_title, (macro,), macro.score, macro.volume_24h)
        ],
    }

    rendered = radar.render_digest_caption(sections, total_count=2, eligible_count=2, limit=2)

    assert "<b>另外一件</b>" in rendered
    assert long_title in rendered
    assert "…" not in rendered


def test_run_is_silent_when_no_item_passes_digest_quality(monkeypatch, tmp_path):
    radar = load_module()
    category = next(category for category in radar.CATEGORIES if category.key == "sports")
    signal = replace(
        synthetic_signal(radar, category, "sports_culture", 0, 90.0),
        translation_status="needs_review",
    )
    item = radar.ReportItem(
        story_key=radar.story_key(signal),
        title=signal.market_title,
        signals=(signal,),
        score=signal.score,
        total_volume_24h=signal.volume_24h,
    )
    collection = radar.CollectionResult(
        signals=[signal],
        rejected=[],
        raw_candidate_count=1,
        source_counts={"test": 1},
    )
    monkeypatch.setattr(radar, "LATEST_PATH", tmp_path / "latest.json")
    monkeypatch.setattr(radar, "HISTORY_PATH", tmp_path / "history.jsonl")
    monkeypatch.setattr(radar, "collect_signal_result", lambda _limit: collection)
    monkeypatch.setattr(radar, "load_display_seen", lambda: {})
    monkeypatch.setattr(
        radar,
        "report_items_by_section",
        lambda _signals, display_seen=None, now=None: {"sports_culture": [item]},
    )
    monkeypatch.setattr(
        radar,
        "deliver_digest",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("low-quality filler must stay silent")
        ),
    )
    args = argparse.Namespace(
        limit=120,
        force=False,
        dry_run=False,
        no_image=False,
        no_llm=True,
        explain=False,
    )

    assert radar.run(args) == 0
    history = json.loads((tmp_path / "history.jsonl").read_text().strip())
    assert history["status"] == "no_quality_digest"


def test_digest_front_page_requires_chinese_cross_section_quality():
    radar = load_module()
    signals = build_fixture_signals(radar, load_fixture_20260618())
    sections = radar.report_items_by_section(signals)

    digest = radar.digest_items_by_interest(sections)

    assert 1 <= len(digest) <= radar.DIGEST_MAIN_ITEMS
    assert len({section_key for section_key, _item in digest}) == len(digest)
    assert all(item.total_volume_24h >= radar.DIGEST_MIN_VOLUME_24H for _section, item in digest)
    assert all(radar.digest_title_quality_ok(item) for _section, item in digest)
    assert all("…" not in radar.digest_event_title_cn(item) for _section, item in digest)


def test_digest_uses_sports_only_after_broad_interest_sections():
    radar = load_module()
    now = datetime(2026, 6, 27, 0, 0, tzinfo=timezone.utc)
    questions = [
        "US recession by end of 2026?",
        "Will Apple release a foldable iPhone before 2027?",
        "Will Samuel Alito announce his retirement by December 31, 2026?",
        "Will Kylian Mbappe be the top goalscorer at the 2026 FIFA World Cup?",
    ]
    signals = []
    for question in questions:
        event = synthetic_event(question)
        signal, rejected = radar.build_signal(event, event["markets"][0], "confirmed", now)
        assert rejected is None
        assert signal is not None
        signals.append(signal)

    digest = radar.digest_items_by_interest(radar.report_items_by_section(signals))

    assert len(digest) == radar.DIGEST_MAIN_ITEMS
    assert all(section_key != "sports_culture" for section_key, _item in digest)


def test_digest_uses_concrete_market_question_instead_of_group_label():
    radar = load_module()
    signals = build_fixture_signals(radar, load_fixture_20260618())
    meeting = next(
        item
        for item in radar.section_report_items(signals, ("geopolitics",))
        if item.story_key.endswith(":us_iran_diplomatic_meeting")
    )

    assert meeting.title == "美国与伊朗外交会谈"
    assert radar.digest_event_title_cn(meeting) != meeting.title
    assert "6月" in radar.digest_event_title_cn(meeting)


def test_visual_bundle_is_source_bound_and_renders_variants(tmp_path):
    radar = load_module()
    signals = build_fixture_signals(radar, load_fixture_20260618())
    digest = radar.digest_items_by_interest(radar.report_items_by_section(signals))
    source = radar.build_visual_source_payload(
        digest,
        total_count=999,
        eligible_count=len(signals),
        timestamp="2026-07-24 15:00 SGT",
    )
    assert source is not None

    script_dir = str(SCRIPT_PATH.parent)
    if script_dir not in sys.path:
        sys.path.insert(0, script_dir)
    visual = __import__("polymarket_radar_visual")

    variants = {
        "normal": dict(source),
        "long-cjk": {
            **source,
            "headline": "美国与伊朗是否会在下一轮外交会谈前公布包含能源、制裁与核查安排的完整协议文本",
        },
        "missing-delta": {**source, "delta_24h": "暂无"},
        "extreme": {**source, "value": "99.9%", "probability": 0.999},
    }
    for name, variant in variants.items():
        bundle = radar.build_visual_bundle(variant)
        result = visual.render_front_page(bundle, tmp_path / f"{name}.png")
        assert result["validation"]["status"] == "verified"
        assert result["font_warning"] is False
        with Image.open(result["image_path"]) as image:
            assert image.width == 1200
            assert image.height >= 1040
            assert image.info["traceability_status"] == "verified"
            assert image.info["text_truncated"] == "false"


def test_interest_score_can_prioritize_curiosity_over_raw_heat():
    radar = load_module()
    categories = {category.key: category for category in radar.CATEGORIES}

    high_heat_sports = replace(
        synthetic_signal(radar, categories["sports"], "sports_culture", 0, 96.0),
        probability=0.995,
        outcome_prices=(("Yes", 0.995), ("No", 0.005)),
        delta_24h=0.01,
        ttl_hours=720,
        volume_24h=1_200_000,
    )
    lower_heat_macro = replace(
        synthetic_signal(radar, categories["macro_finance"], "macro_finance", 1, 64.0),
        probability=0.52,
        outcome_prices=(("Yes", 0.52), ("No", 0.48)),
        delta_24h=0.22,
        ttl_hours=48,
        volume_24h=180_000,
    )
    sports_item = radar.ReportItem(
        story_key=radar.story_key(high_heat_sports),
        title=radar.market_title_cn(high_heat_sports),
        signals=(high_heat_sports,),
        score=high_heat_sports.score,
        total_volume_24h=high_heat_sports.volume_24h,
    )
    macro_item = radar.ReportItem(
        story_key=radar.story_key(lower_heat_macro),
        title=radar.market_title_cn(lower_heat_macro),
        signals=(lower_heat_macro,),
        score=lower_heat_macro.score,
        total_volume_24h=lower_heat_macro.volume_24h,
    )

    assert radar.interest_score(macro_item, "macro_finance") > radar.interest_score(sports_item, "sports_culture")


def test_report_items_by_section_keeps_three_per_section_when_available():
    radar = load_module()
    categories = {category.key: category for category in radar.CATEGORIES}
    signals = []
    for section_index, (section_key, _section_label, category_keys) in enumerate(radar.SECTION_DEFS):
        category = categories[category_keys[0]]
        for item_index in range(radar.BASE_SECTION_DISPLAY_ITEMS):
            score = 90.0 - section_index - (item_index / 10.0)
            signals.append(synthetic_signal(radar, category, section_key, item_index, score))

    sections = radar.report_items_by_section(signals)

    # V3.4 起 7 板块 × 3 基础位 = 21 超过 TARGET_DISPLAY_ITEMS(18)，
    # 分配器按 SECTION_DEFS 顺序 round-robin，尾部板块被挤压。
    expected_total = min(
        radar.TARGET_DISPLAY_ITEMS,
        radar.BASE_SECTION_DISPLAY_ITEMS * len(radar.SECTION_DEFS),
    )
    assert radar.display_slot_count() == radar.TARGET_DISPLAY_ITEMS
    assert radar.display_item_total(sections) == expected_total
    base_rounds, extra = divmod(expected_total, len(radar.SECTION_DEFS))
    expected_counts = {
        section_key: min(
            radar.BASE_SECTION_DISPLAY_ITEMS,
            base_rounds + (1 if index < extra else 0),
        )
        for index, (section_key, _label, _keys) in enumerate(radar.SECTION_DEFS)
    }
    assert {
        section_key: len(sections[section_key])
        for section_key, _section_label, _category_keys in radar.SECTION_DEFS
    } == expected_counts


def test_report_items_by_section_skips_recent_seen_and_backfills():
    radar = load_module()
    categories = {category.key: category for category in radar.CATEGORIES}
    now = datetime(2026, 6, 27, 0, 0, tzinfo=timezone.utc)
    signals = []
    recent_story_keys = set()

    for section_index, (section_key, _section_label, category_keys) in enumerate(radar.SECTION_DEFS):
        category = categories[category_keys[0]]
        for item_index in range(radar.BASE_SECTION_DISPLAY_ITEMS + 1):
            score = 25.0 if item_index == radar.BASE_SECTION_DISPLAY_ITEMS else 90.0 - section_index - (item_index / 10.0)
            signal = synthetic_signal(radar, category, section_key, item_index, score)
            signals.append(signal)
            if item_index == 0:
                recent_story_keys.add(radar.story_key(signal))

    display_seen = {
        key: {"sent_at": now.isoformat(), "title": key}
        for key in recent_story_keys
    }

    sections = radar.report_items_by_section(signals, display_seen=display_seen, now=now)
    selected_story_keys = {
        item.story_key
        for items in sections.values()
        for item in items
    }

    expected_total = min(
        radar.TARGET_DISPLAY_ITEMS,
        radar.BASE_SECTION_DISPLAY_ITEMS * len(radar.SECTION_DEFS),
    )
    assert radar.display_item_total(sections) == expected_total
    assert not selected_story_keys & recent_story_keys
    assert radar.recent_display_story_count(signals, display_seen, now=now) == len(recent_story_keys)


def test_new_scope_templates_keep_titles_chinese_and_categories_clean():
    radar = load_module()
    now = datetime(2026, 6, 27, 0, 0, tzinfo=timezone.utc)

    cases = [
        (
            "Will 40 ships transit the Strait of Hormuz on any day by June 30, 2026?",
            "Strait of Hormuz shipping",
            "geopolitics",
            "霍尔木兹海峡是否在2026年6月30日前单日通行量达到40艘船",
        ),
        (
            "Will Diana DeGette be the Democratic nominee for CO-01?",
            "CO-01 Democratic Primary",
            "politics_policy",
            "戴安娜·德格特是否成为科罗拉多州第1选区民主党提名人",
        ),
        (
            "Will Tesla deliver 475000 or more vehicles in Q2 2026",
            "Tesla Q2 deliveries",
            "tech_business",
            "特斯拉在2026年第2季度交付量是否达到475000辆或以上",
        ),
        (
            "Will Iran announce withdrawal from MOU negotiations by July 31?",
            "Iran MOU negotiations",
            "geopolitics",
            "伊朗是否在7月31日前宣布退出谅解备忘录谈判",
        ),
        (
            "Iran charges Hormuz fees by August 31?",
            "Hormuz fees",
            "geopolitics",
            "伊朗是否在8月31日前向霍尔木兹海峡通行收费",
        ),
        (
            "Will Wes Streeting be the next Foreign Secretary of the UK?",
            "UK cabinet politics",
            "politics_policy",
            "韦斯·斯特里廷是否成为英国下一任外交大臣",
        ),
        (
            "Will Samuel Alito announce his retirement by December 31, 2026?",
            "Supreme Court retirement",
            "politics_policy",
            "塞缪尔·阿利托是否在2026年12月31日前宣布退休",
        ),
        (
            "Will Mitch McConnell step down from the Senate before his term ends?",
            "US Senate politics",
            "politics_policy",
            "米奇·麦康奈尔是否在任期结束前辞去参议员职务",
        ),
        (
            "Will Kylian Mbappe be the top goalscorer at the 2026 FIFA World Cup?",
            "World Cup top goalscorer",
            "sports",
            "基利安·姆巴佩是否成为2026年世界杯最佳射手",
        ),
        (
            "US recession by end of 2026?",
            "US recession",
            "macro_finance",
            "美国是否在2026年底前出现经济衰退",
        ),
        (
            "Gold (XAUUSD) hit (LOW) $3,600 in July?",
            "Gold price",
            "macro_finance",
            "黄金在7月是否触及3,600美元（低点）",
        ),
        (
            "Silver (XAGUSD) hit (HIGH) $63 week of July 6 2026?",
            "Silver price",
            "macro_finance",
            "白银在2026年7月6日是否触及63美元（高点）",
        ),
        (
            "Will MetaMask launch a token by September 30, 2026?",
            "MetaMask token launch",
            "crypto_web3",
            "MetaMask是否在2026年9月30日前发行代币",
        ),
        (
            "Will the next Google Gemini Pro model be released on July 17, 2026?",
            "Google Gemini model release",
            "tech_business",
            "下一代谷歌 Gemini Pro模型是否在2026年7月17日发布",
        ),
        (
            "Gavin Newsom or his wife federally charged by December 31, 2026?",
            "California governor federal charge politics",
            "politics_policy",
            "加文·纽森或其妻子是否在2026年12月31日前受到联邦指控",
        ),
        (
            "Will China GDP growth in Q2 2026 be between 4.3% and 4.6%?",
            "China GDP",
            "macro_finance",
            "中国2026年第2季度GDP增速是否为4.3%-4.6%",
        ),
        (
            "Will Apple release a foldable iPhone before 2027?",
            "Apple foldable iPhone",
            "tech_business",
            "苹果是否在2027年前发布折叠屏 iPhone",
        ),
        (
            "Will SpaceX have the highest IPO market cap 2026?",
            "SpaceX IPO market cap",
            "tech_business",
            "太空探索技术公司是否成为2026年IPO市值最高公司",
        ),
        (
            "Will Benjamin Netanyahu be the next Prime Minister of Israel?",
            "Israel prime minister politics",
            "politics_policy",
            "本雅明·内塔尼亚胡是否成为以色列下一任总理",
        ),
        (
            "Will United Russia (ER) gain the most seats in the next Russian parliamentary election?",
            "Russian parliamentary election",
            "politics_policy",
            "统一俄罗斯党是否在下一次俄罗斯议会选举中获得最多席位",
        ),
    ]

    for question, title, category_key, expected_title in cases:
        event = synthetic_event(question, title=title)
        signal, rejected = radar.build_signal(event, event["markets"][0], "confirmed", now)
        assert rejected is None
        assert signal is not None
        assert signal.category.key == category_key
        assert radar.market_title_cn(signal) == expected_title


# ---------------------------------------------------------------------------
# V3.1 — correctness fixes
# ---------------------------------------------------------------------------


def test_market_volume_24h_ignores_cumulative_lifetime_volume():
    radar = load_module()
    dormant_but_famous = {
        "volume24hr": 30_000,
        "volume24hrClob": 28_000,
        "volume": 39_000_000,
        "volumeNum": 39_000_000,
    }

    assert radar.market_volume_24h(dormant_but_famous) == 30_000


def test_question_deadline_rolls_into_the_next_year():
    radar = load_module()
    december = datetime(2026, 12, 20, 0, 0, tzinfo=timezone.utc)

    deadline, source = radar.infer_question_deadline("Will the ETF be approved by January 5?", december)

    assert source == "question.by_date"
    assert deadline is not None
    assert deadline.year == 2027
    assert deadline > december


def test_question_deadline_survives_an_impossible_date():
    radar = load_module()
    now = datetime(2026, 1, 10, 0, 0, tzinfo=timezone.utc)

    deadline, source = radar.infer_question_deadline("Will it ship by February 30?", now)

    assert (deadline, source) == (None, None)


def test_only_transport_errors_take_the_curl_retry_path():
    radar = load_module()
    import requests as requests_module

    bad_request = requests_module.exceptions.HTTPError()
    bad_request.response = type("R", (), {"status_code": 400})()
    server_error = requests_module.exceptions.HTTPError()
    server_error.response = type("R", (), {"status_code": 502})()

    assert radar.is_transport_error(requests_module.exceptions.ConnectionError()) is True
    assert radar.is_transport_error(requests_module.exceptions.Timeout()) is True
    assert radar.is_transport_error(server_error) is True
    assert radar.is_transport_error(bad_request) is False
    assert radar.is_transport_error(ValueError("caption too long")) is False


def test_assess_source_health_separates_blind_from_quiet():
    radar = load_module()
    radar.reset_fetch_failures()

    healthy = radar.assess_source_health({"events": [{}]}, {"markets": [{}]}, 2)
    partial = radar.assess_source_health({"events": [{}], "keyset": []}, {"markets": [{}]}, 1)
    blind = radar.assess_source_health({"events": [], "keyset": []}, {"markets": [{}]}, 1)
    empty = radar.assess_source_health({"events": [{}]}, {"markets": [{}]}, 0)

    assert healthy["status"] == "ok"
    assert partial["status"] == "degraded"
    assert "empty_sources:keyset" in partial["reasons"]
    assert blind["status"] == "failed"
    assert "all_event_sources_empty" in blind["reasons"]
    assert empty["status"] == "failed"
    assert "no_candidates" in empty["reasons"]


def test_run_exits_nonzero_and_alerts_when_every_source_is_blind(monkeypatch, tmp_path):
    radar = load_module()
    collection = radar.CollectionResult(
        signals=[],
        rejected=[],
        raw_candidate_count=0,
        source_counts={"events": 0, "markets": 0},
        source_health={"status": "failed", "reasons": ["no_candidates"], "fetch_failure_count": 3},
    )
    alerts = []
    monkeypatch.setattr(radar, "LATEST_PATH", tmp_path / "latest.json")
    monkeypatch.setattr(radar, "HISTORY_PATH", tmp_path / "history.jsonl")
    monkeypatch.setattr(radar, "collect_signal_result", lambda _limit: collection)
    monkeypatch.setattr(radar, "notify_source_failure", lambda health, dry_run: alerts.append(health))
    args = argparse.Namespace(
        limit=120, force=False, dry_run=True, no_image=True, no_llm=True, explain=False
    )

    assert radar.run(args) == 1
    history = json.loads((tmp_path / "history.jsonl").read_text().strip())
    assert history["status"] == "source_failure"
    assert len(alerts) == 1


def test_monitor_alert_is_deduped_within_the_same_day(monkeypatch, tmp_path):
    radar = load_module()
    monkeypatch.setattr(radar, "SOURCE_ALERT_STATE_PATH", tmp_path / "alert.json")
    now = datetime(2026, 7, 25, 3, 0, tzinfo=timezone.utc)

    first = radar.alert_already_sent_today("source_failure", now)
    second = radar.alert_already_sent_today("source_failure", now)
    other_kind = radar.alert_already_sent_today("send_failed", now)
    next_day = radar.alert_already_sent_today("source_failure", now.replace(day=26))

    assert (first, second, other_kind, next_day) == (False, True, False, False)


def test_sports_digest_gate_uses_the_calibrated_volume_threshold():
    radar = load_module()

    assert radar.DIGEST_SPORTS_MIN_VOLUME_24H < 1_000_000
    assert radar.DIGEST_SPORTS_MIN_VOLUME_24H > radar.DIGEST_MIN_VOLUME_24H


def test_headline_size_falls_back_instead_of_raising():
    script_dir = str(SCRIPT_PATH.parent)
    if script_dir not in sys.path:
        sys.path.insert(0, script_dir)
    visual = __import__("polymarket_radar_visual")
    layout = __import__("tg_watch_layout")

    unrenderable = "美国" * 200
    size = visual._headline_size(unrenderable, 1060)
    lines = layout.wrap_text(unrenderable, size, True, 1060, 2)

    assert size == 48
    assert lines[-1].endswith("…")


# ---------------------------------------------------------------------------
# V3.2 — quality
# ---------------------------------------------------------------------------


def test_parse_llm_translation_payload_handles_fenced_json():
    radar = load_module()
    fenced = '```json\n[{"i": 0, "cn": "美联储是否9月降息"}, {"i": 1, "cn": "俄乌是否停火"}]\n```'

    assert radar.parse_llm_translation_payload(fenced) == {
        0: "美联储是否9月降息",
        1: "俄乌是否停火",
    }
    assert radar.parse_llm_translation_payload("sorry, I cannot help with that") == {}


def test_llm_translation_acceptance_rejects_untranslated_output():
    radar = load_module()
    english = "Will the Fed cut rates in September?"

    assert radar.llm_translation_acceptable("美联储是否在9月降息", english) is True
    assert radar.llm_translation_acceptable(english, english) is False
    assert radar.llm_translation_acceptable("Will the Fed cut rates 是否", english) is False
    assert radar.llm_translation_acceptable("", english) is False


def test_apply_llm_translations_uses_cache_and_leaves_scores_alone(monkeypatch, tmp_path):
    radar = load_module()
    category = next(category for category in radar.CATEGORIES if category.key == "macro_finance")
    signal = replace(
        synthetic_signal(radar, category, "macro_finance", 0, 61.0),
        market_title="Will the Fed cut rates by 50 bps in September?",
        translation_status="needs_review",
    )
    cache_path = tmp_path / "translations.json"
    cache_path.write_text(
        json.dumps(
            {
                radar.translation_cache_key(signal.market_title): {
                    "cn": "美联储是否在9月降息50个基点",
                    "ts": "2026-07-25T00:00:00+00:00",
                    "model": "test",
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(radar, "TRANSLATION_CACHE_PATH", cache_path)
    monkeypatch.setattr(
        radar,
        "request_llm_translations",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("cache hit must not call the LLM")),
    )

    stats = apply_and_return(radar, [signal])

    assert stats["cache_hits"] == 1
    assert signal.score == 61.0
    assert signal.translation_engine == "llm"
    assert signal.translation_status == "ok"
    assert radar.market_title_cn(signal) == "美联储是否在9月降息50个基点"


def apply_and_return(radar, signals):
    return radar.apply_llm_translations(
        signals,
        enabled=True,
        # The public build requires an explicit endpoint; the HTTP layer is
        # monkeypatched in these tests so the URL is never actually dialed.
        env={"CLIPROXY_API_KEY": "unused-in-test", "CLIPROXY_BASE_URL": "http://127.0.0.1:9"},
    )


def test_apply_llm_translations_survives_an_unreachable_endpoint(monkeypatch, tmp_path):
    radar = load_module()
    category = next(category for category in radar.CATEGORIES if category.key == "macro_finance")
    signal = replace(
        synthetic_signal(radar, category, "macro_finance", 0, 61.0),
        translation_status="needs_review",
    )
    monkeypatch.setattr(radar, "TRANSLATION_CACHE_PATH", tmp_path / "translations.json")
    monkeypatch.setattr(
        radar,
        "request_llm_translations",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(TimeoutError("no route to host")),
    )

    stats = apply_and_return(radar, [signal])

    assert stats["status"] == "error"
    assert signal.translation_engine == "rule"
    assert signal.translation_status == "needs_review"


def test_a_late_chunk_failing_keeps_the_titles_an_earlier_chunk_translated(monkeypatch, tmp_path):
    radar = load_module()
    monkeypatch.setattr(radar, "LLM_BATCH_SIZE", 2)
    category = next(category for category in radar.CATEGORIES if category.key == "macro_finance")
    signals = [
        replace(
            synthetic_signal(radar, category, "macro_finance", index, 90.0 - index),
            market_title=f"Will event {index} happen in July?",
            translation_status="needs_review",
        )
        for index in range(4)
    ]
    monkeypatch.setattr(radar, "TRANSLATION_CACHE_PATH", tmp_path / "translations.json")
    calls: list[list[str]] = []

    def flaky(titles, _settings):
        calls.append(list(titles))
        if len(calls) > 1:
            raise TimeoutError("timed out")
        return {index: f"事件{index}是否在7月发生" for index in range(len(titles))}

    monkeypatch.setattr(radar, "request_llm_translations", flaky)

    stats = apply_and_return(radar, signals)

    assert [len(chunk) for chunk in calls] == [2, 2]
    assert stats["status"] == "partial"
    assert stats["llm_applied"] == 2
    assert [signal.translation_engine for signal in signals] == ["llm", "llm", "rule", "rule"]


def test_display_score_floor_is_enforced_when_a_section_is_rich():
    radar = load_module()
    categories = {category.key: category for category in radar.CATEGORIES}
    section_key, _label, category_keys = radar.SECTION_DEFS[0]
    category = categories[category_keys[0]]
    signals = [
        synthetic_signal(radar, category, section_key, index, score)
        for index, score in enumerate((90.0, 80.0, 70.0, 20.0))
    ]
    # Any cooldown history at all used to drop every floor to the diversity
    # tier, which let the score-20 filler onto the page.
    unrelated_seen = {"some:other:story": {"sent_at": datetime.now(timezone.utc).isoformat()}}

    sections = radar.report_items_by_section(signals, display_seen=unrelated_seen)
    scores = sorted(item.score for item in sections[section_key])

    assert len(scores) == radar.BASE_SECTION_DISPLAY_ITEMS
    assert min(scores) >= radar.DISPLAY_SCORE


def test_thin_section_relaxes_the_floor_rather_than_showing_nothing():
    radar = load_module()
    categories = {category.key: category for category in radar.CATEGORIES}
    section_key, _label, category_keys = radar.SECTION_DEFS[0]
    category = categories[category_keys[0]]
    weak = synthetic_signal(radar, category, section_key, 0, radar.DIVERSITY_DISPLAY_SCORE + 1.0)

    sections = radar.report_items_by_section([weak])

    assert [item.score for item in sections[section_key]] == [weak.score]


def test_rotate_history_keeps_only_the_newest_records(tmp_path):
    radar = load_module()
    history = tmp_path / "history.jsonl"
    history.write_text("".join(f'{{"n": {index}}}\n' for index in range(5_000)), encoding="utf-8")

    rotated = radar.rotate_history(history, max_bytes=1024, keep=10)
    lines = history.read_text(encoding="utf-8").strip().splitlines()

    assert rotated is True
    assert len(lines) == 10
    assert json.loads(lines[-1])["n"] == 4_999
    assert history.with_suffix(".jsonl.1").exists()


def test_rotate_history_leaves_a_small_log_alone(tmp_path):
    radar = load_module()
    history = tmp_path / "history.jsonl"
    history.write_text('{"n": 1}\n', encoding="utf-8")

    assert radar.rotate_history(history, max_bytes=1024, keep=10) is False
    assert history.read_text(encoding="utf-8") == '{"n": 1}\n'


def test_translation_cache_is_pruned_by_age_and_size():
    radar = load_module()
    now = datetime(2026, 7, 25, 0, 0, tzinfo=timezone.utc)
    cache = {
        "fresh": {"cn": "新鲜", "ts": now.isoformat()},
        "stale": {"cn": "过期", "ts": "2026-01-01T00:00:00+00:00"},
        "broken": {"ts": now.isoformat()},
    }

    pruned = radar.prune_translation_cache(cache, now=now)

    assert set(pruned) == {"fresh"}


def test_telegram_request_errors_redact_bot_token(monkeypatch, tmp_path):
    radar = load_module()
    image_path = tmp_path / "radar.png"
    image_path.write_bytes(b"png")
    token = "123456789:super-secret-token-value"

    class FailingRequests:
        # ValueError 不算 transport 错误，走的是不重试 curl 的直抛分支——
        # 正是修复前会把含 token 的原始 URL 泄进 launchd err.log 的路径。
        @staticmethod
        def post(url, **_kwargs):
            raise ValueError(url)

    monkeypatch.setattr(radar, "requests", FailingRequests)

    with pytest.raises(RuntimeError) as error:
        radar.send_photo(token, "-100", 412, image_path, "摘要")

    assert token not in str(error.value)
    assert "<redacted>" in str(error.value)


def test_curl_send_errors_redact_bot_token(monkeypatch):
    radar = load_module()
    token = "123456789:super-secret-token-value"
    failed = SimpleNamespace(
        returncode=7,
        stdout="",
        stderr=f"curl: (7) Failed to connect: https://api.telegram.org/bot{token}/sendMessage",
    )

    monkeypatch.setattr(radar, "requests", None)
    monkeypatch.setattr(radar.subprocess, "run", lambda *args, **kwargs: failed)

    with pytest.raises(RuntimeError) as error:
        radar.send_message(token, "-100", 412, "hello")

    assert token not in str(error.value)
    assert "<redacted>" in str(error.value)


def test_telegram_target_requires_explicit_public_configuration():
    radar = load_module()

    for env, missing_key in (
        ({}, "TELEGRAM_BOT_TOKEN"),
        ({"TELEGRAM_BOT_TOKEN": "token"}, "POLYMARKET_CHAT_ID"),
        (
            {
                "TELEGRAM_BOT_TOKEN": "token",
                "POLYMARKET_CHAT_ID": "-100123",
            },
            "POLYMARKET_TOPIC_ID",
        ),
    ):
        try:
            radar.telegram_target(env)
        except RuntimeError as exc:
            assert missing_key in str(exc)
        else:
            raise AssertionError(f"{missing_key} should be required")

    assert radar.telegram_target(
        {
            "TELEGRAM_BOT_TOKEN": "token",
            "POLYMARKET_CHAT_ID": "-100123",
            "POLYMARKET_TOPIC_ID": "412",
        }
    ) == ("token", "-100123", 412)


def test_invalid_json_state_fails_closed(tmp_path):
    radar = load_module()
    state_path = tmp_path / "seen.json"
    state_path.write_text("{")

    with pytest.raises(RuntimeError, match="Invalid JSON state"):
        radar.read_json(state_path, {})


def test_single_instance_lock_rejects_second_holder(tmp_path):
    radar = load_module()
    lock_path = tmp_path / "run.lock"

    with radar.single_instance_lock(lock_path):
        with pytest.raises(RuntimeError, match="already running"):
            with radar.single_instance_lock(lock_path):
                pass
