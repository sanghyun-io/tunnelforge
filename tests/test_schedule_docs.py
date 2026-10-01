from pathlib import Path
import re


PROJECT_ROOT = Path(__file__).resolve().parents[1]


README_CONTRACTS = {
    "README.md": {
        "features_heading": "Features",
        "tips_heading": "Tips",
        "disabled_status": "Scheduled SQL execution is not supported",
        "reactivation_status": "backup tasks only",
    },
    "README.ko.md": {
        "features_heading": "주요 기능",
        "tips_heading": "사용 팁",
        "disabled_status": "예약 SQL 실행은 지원하지 않습니다",
        "reactivation_status": "백업만",
    },
}

TABLE_ITEM_PATTERN = r"^\s*\|?[^|\n]+(?:\|[^|\n]*)+\|?\s*$"
LIST_ITEM_PATTERN = r"^\s*(?:[-*+] |\d+\. )"


def _extract_h2_section(document, heading):
    match = re.search(
        rf"(?ms)^## {re.escape(heading)}\s*$\n(?P<section>.*?)(?=^## |\Z)",
        document,
    )
    assert match, f"missing README section: {heading}"
    return match.group("section")


def _lines_matching_schedule_claims(section, line_pattern):
    return [
        line
        for line in section.splitlines()
        if re.match(line_pattern, line) and re.search(
            r"schedule|예약(?=\s|$|[&*_])|스케줄", line, re.IGNORECASE
        )
    ]


def test_schedule_claim_matcher_catches_common_table_and_tip_variants():
    table_variants = [
        "| Scheduled Backups | ... |",
        "Scheduled Backups | ...",
        "| 예약 백업 | ...",
        "스케줄 백업 | ... |",
    ]
    tip_variants = [
        "- Use schedules ...",
        "* 예약 작업 ...",
        "1. 스케줄 백업 ...",
    ]

    for line in table_variants:
        assert _lines_matching_schedule_claims(line, TABLE_ITEM_PATTERN) == [line]
    for line in tip_variants:
        assert _lines_matching_schedule_claims(line, LIST_ITEM_PATTERN) == [line]

    assert not _lines_matching_schedule_claims(
        "Scheduled Backups are disabled in the default UI. See SCHEDULE.md.",
        TABLE_ITEM_PATTERN,
    )
    assert not _lines_matching_schedule_claims(
        "See [SCHEDULE.md](SCHEDULE.md) for the current status.",
        LIST_ITEM_PATTERN,
    )


def test_bilingual_readmes_describe_scheduled_backups_as_backup_only():
    for filename, contract in README_CONTRACTS.items():
        doc = (PROJECT_ROOT / filename).read_text(encoding="utf-8")
        feature_section = _extract_h2_section(doc, contract["features_heading"])
        tips_section = _extract_h2_section(doc, contract["tips_heading"])

        assert not _lines_matching_schedule_claims(feature_section, TABLE_ITEM_PATTERN)
        assert not _lines_matching_schedule_claims(tips_section, LIST_ITEM_PATTERN)
        assert contract["disabled_status"] in doc
        assert contract["reactivation_status"] in doc
        assert "SCHEDULE.md" in doc


def test_schedule_guide_states_the_backup_only_scope_and_the_safety_rules():
    doc = (PROJECT_ROOT / "SCHEDULE.md").read_text(encoding="utf-8")

    assert "예약 SQL 실행은 지원하지 않습니다" in doc
    assert "현재 메인 UI에서 비활성화" not in doc
    for rule in ("존재하지 않는 시각", "반복되는 시각", "최대 1회", "자동 수락하지 않고 실패",
                 "소유 마커", "마커가 없는 폴더", "가장 최근 완료 백업은 항상 남깁니다"):
        assert rule in doc
