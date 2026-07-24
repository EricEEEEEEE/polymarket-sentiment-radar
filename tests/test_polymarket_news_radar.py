from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "polymarket_news_radar.py"
FIXTURE_PATH = ROOT / "tests" / "fixtures" / "polymarket_news_radar_fixture_20260617.json"
FIXTURE_20260618_PATH = ROOT / "tests" / "fixtures" / "polymarket_news_radar_fixture_20260618.json"


def load_module():
    spec = importlib.util.spec_from_file_location("polymarket_news_radar", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["polymarket_news_radar"] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


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

    rendered = radar.render_text_message(signals, total_count=999, selected_count=6, eligible_count=len(signals))

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
    monkeypatch.setattr(radar, "select_sendable", lambda _signals, force=False: ([signal], []))
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

    expected_total = radar.BASE_SECTION_DISPLAY_ITEMS * len(radar.SECTION_DEFS)
    assert radar.display_slot_count() == expected_total
    assert radar.display_item_total(sections) == expected_total
    assert {
        section_key: len(sections[section_key])
        for section_key, _section_label, _category_keys in radar.SECTION_DEFS
    } == {section_key: radar.BASE_SECTION_DISPLAY_ITEMS for section_key, _label, _keys in radar.SECTION_DEFS}


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

    expected_total = radar.BASE_SECTION_DISPLAY_ITEMS * len(radar.SECTION_DEFS)
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
