from unittest.mock import MagicMock, patch

from src.core.update_downloader import UpdateDownloader, select_release_asset


def test_select_release_asset_prefers_windows_offline_installer():
    assets = [
        {"name": "TunnelForge-WebSetup.exe", "browser_download_url": "web", "size": 1},
        {"name": "TunnelForge-Setup-2.0.5.exe", "browser_download_url": "setup", "size": 2},
        {"name": "TunnelForge-macOS-2.0.5.dmg", "browser_download_url": "dmg", "size": 3},
    ]

    asset = select_release_asset(assets, platform_name="Windows")

    assert asset == ("setup", 2)


def test_select_release_asset_prefers_macos_dmg():
    assets = [
        {"name": "TunnelForge-macOS-2.0.5.zip", "browser_download_url": "zip", "size": 2},
        {"name": "TunnelForge-macOS-2.0.5.dmg", "browser_download_url": "dmg", "size": 3},
        {"name": "TunnelForge-Setup-2.0.5.exe", "browser_download_url": "setup", "size": 4},
    ]

    asset = select_release_asset(assets, platform_name="Darwin")

    assert asset == ("dmg", 3)


def test_select_release_asset_prefers_matching_macos_architecture():
    assets = [
        {"name": "TunnelForge-macOS-2.0.5-x86_64.dmg", "browser_download_url": "intel", "size": 2},
        {"name": "TunnelForge-macOS-2.0.5-arm64.zip", "browser_download_url": "arm-zip", "size": 3},
        {"name": "TunnelForge-macOS-2.0.5-arm64.dmg", "browser_download_url": "arm-dmg", "size": 4},
    ]

    asset = select_release_asset(assets, platform_name="Darwin", arch_name="arm64")

    assert asset == ("arm-dmg", 4)


def test_select_release_asset_ignores_other_macos_architecture():
    assets = [
        {"name": "TunnelForge-macOS-2.0.5-x86_64.dmg", "browser_download_url": "intel", "size": 2},
        {"name": "TunnelForge-macOS-2.0.5-arm64.zip", "browser_download_url": "arm-zip", "size": 3},
    ]

    asset = select_release_asset(assets, platform_name="Darwin", arch_name="arm64")

    assert asset == ("arm-zip", 3)


def test_select_release_asset_returns_none_when_platform_asset_missing():
    assets = [
        {"name": "TunnelForge-Setup-2.0.5.exe", "browser_download_url": "setup", "size": 4},
    ]

    assert select_release_asset(assets, platform_name="Darwin") is None


def test_download_installer_uses_configurable_timeout():
    """download_installer가 하드코딩된 30초 대신 self.timeout을 전달해야 한다 (CC-048 회귀)"""
    downloader = UpdateDownloader()
    downloader.download_url = "https://example.com/TunnelForge-Setup-2.0.5.exe"
    downloader.file_size = 10

    config_manager = MagicMock()
    config_manager.get_network_timeout_download.return_value = 77
    downloader._config_manager = config_manager

    mock_response = MagicMock()
    mock_response.headers = {'content-length': '10'}
    mock_response.iter_content.return_value = [b'0123456789']

    with patch('src.core.update_downloader.requests.get', return_value=mock_response) as mock_get, \
         patch('src.core.update_downloader.open', create=True):
        downloader.download_installer()

    assert mock_get.call_args.kwargs['timeout'] == 77
