import sys

from src.core.resources import app_icon_path


def test_app_icon_path_uses_png_fallback_on_macos_when_icns_missing(tmp_path):
    assets = tmp_path / "assets"
    assets.mkdir()
    png = assets / "icon.png"
    png.write_text("png", encoding="utf-8")

    result = app_icon_path(base_dir=tmp_path, platform_name="Darwin")

    assert result == png


def test_app_icon_path_uses_ico_on_windows(tmp_path):
    assets = tmp_path / "assets"
    assets.mkdir()
    ico = assets / "icon.ico"
    ico.write_text("ico", encoding="utf-8")
    (assets / "icon.png").write_text("png", encoding="utf-8")

    result = app_icon_path(base_dir=tmp_path, platform_name="Windows")

    assert result == ico


def test_app_icon_path_finds_macos_bundle_resources(monkeypatch, tmp_path):
    contents = tmp_path / "TunnelForge.app" / "Contents"
    frameworks = contents / "Frameworks"
    resources = contents / "Resources"
    assets = resources / "assets"
    assets.mkdir(parents=True)
    icon = assets / "icon.icns"
    icon.write_text("icns", encoding="utf-8")

    monkeypatch.setattr(sys, "_MEIPASS", str(frameworks), raising=False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(contents / "MacOS" / "TunnelForge"))

    result = app_icon_path(platform_name="Darwin")

    assert result == icon
