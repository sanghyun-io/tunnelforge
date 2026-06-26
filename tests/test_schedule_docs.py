from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


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
