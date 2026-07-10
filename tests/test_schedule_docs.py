from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


README_CONTRACTS = {
    "README.md": {
        "current_feature_claim": "| ⏰ | **Scheduled Backups & Queries** | Cron-based automation",
        "current_tip": "Set up a **scheduled backup**",
        "disabled_status": "disabled in the default UI",
        "reactivation_status": "intentional reactivation and verification",
    },
    "README.ko.md": {
        "current_feature_claim": "| ⏰ | **예약 백업 & 쿼리 실행** | Cron 기반",
        "current_tip": "**예약 백업**으로 자동화",
        "disabled_status": "기본 UI에서 비활성화",
        "reactivation_status": "의도적인 재활성화와 검증",
    },
}


def test_bilingual_readmes_describe_schedule_as_unavailable_until_verified():
    for filename, contract in README_CONTRACTS.items():
        doc = (PROJECT_ROOT / filename).read_text(encoding="utf-8")

        assert contract["current_feature_claim"] not in doc
        assert contract["current_tip"] not in doc
        assert contract["disabled_status"] in doc
        assert contract["reactivation_status"] in doc
        assert "SCHEDULE.md" in doc


def test_schedule_guide_does_not_present_hidden_feature_as_public_ui():
    doc = (PROJECT_ROOT / "SCHEDULE.md").read_text(encoding="utf-8")

    assert "현재 메인 UI에서 비활성화" in doc
    assert "재활성화 후 UI 확인 항목" in doc

    public_ui_phrases = [
        '메인 툴바에서 **"스케줄"** 버튼을 클릭',
        "스케줄 시간을 기다리지 않고 바로 백업하려면:",
        "스케줄 관리 창의 **\"백업 로그\"** 탭에서",
        "스케줄이 작동하려면 TunnelForge가 실행 중이어야 합니다.",
    ]

    for phrase in public_ui_phrases:
        assert phrase not in doc
