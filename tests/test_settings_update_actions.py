from src.ui.dialogs.settings import update_package_action_text


def test_update_package_action_text_uses_open_wording_for_macos_packages():
    text = update_package_action_text("open")

    assert text.button == "📂 패키지 열기"
    assert "패키지 열기" in text.done_message
    assert "설치 시작" not in text.button
    assert "설치 시작" not in text.done_message
    assert "다운로드한 패키지를 열면 현재 앱이 종료됩니다." in text.confirm_body


def test_update_package_action_text_keeps_installer_wording_for_windows():
    text = update_package_action_text("execute")

    assert text.button == "🚀 설치 시작"
    assert "설치 시작" in text.done_message
    assert "설치를 위해 현재 앱이 종료됩니다." in text.confirm_body
