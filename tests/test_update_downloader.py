import hashlib
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src import update_integrity
from src.core.update_downloader import DownloadError, UpdateDownloader, select_release_asset
from src.ui.workers.update_worker import UpdateDownloadWorker


VALID_DIGEST = "sha256:" + "a" * 64


def _asset(name, url, size, digest=VALID_DIGEST):
    return {
        "name": name,
        "browser_download_url": url,
        "size": size,
        "digest": digest,
    }


def test_select_release_asset_prefers_windows_offline_installer():
    assets = [
        _asset("TunnelForge-WebSetup-2.0.5.exe", "web", 1),
        _asset("TunnelForge-Setup-2.0.5.exe", "setup", 2),
        _asset("TunnelForge-macOS-2.0.5.dmg", "dmg", 3),
    ]

    asset = select_release_asset(assets, "2.0.5", platform_name="Windows")

    assert asset.name == "TunnelForge-Setup-2.0.5.exe"
    assert asset.url == "setup"
    assert asset.size == 2


def test_select_release_asset_prefers_macos_dmg():
    assets = [
        _asset("TunnelForge-macOS-2.0.5.zip", "zip", 2),
        _asset("TunnelForge-macOS-2.0.5.dmg", "dmg", 3),
        _asset("TunnelForge-Setup-2.0.5.exe", "setup", 4),
    ]

    asset = select_release_asset(assets, "2.0.5", platform_name="Darwin")

    assert asset.url == "dmg"
    assert asset.size == 3


def test_select_release_asset_prefers_matching_macos_architecture():
    assets = [
        _asset("TunnelForge-macOS-2.0.5-x86_64.dmg", "intel", 2),
        _asset("TunnelForge-macOS-2.0.5-arm64.zip", "arm-zip", 3),
        _asset("TunnelForge-macOS-2.0.5-arm64.dmg", "arm-dmg", 4),
    ]

    asset = select_release_asset(
        assets, "2.0.5", platform_name="Darwin", arch_name="arm64"
    )

    assert asset.url == "arm-dmg"
    assert asset.size == 4


def test_select_release_asset_ignores_other_macos_architecture():
    assets = [
        _asset("TunnelForge-macOS-2.0.5-x86_64.dmg", "intel", 2),
        _asset("TunnelForge-macOS-2.0.5-arm64.zip", "arm-zip", 3),
    ]

    asset = select_release_asset(
        assets, "2.0.5", platform_name="Darwin", arch_name="arm64"
    )

    assert asset.url == "arm-zip"
    assert asset.size == 3


def test_select_release_asset_returns_none_when_platform_asset_missing():
    assets = [
        _asset("TunnelForge-Setup-2.0.5.exe", "setup", 4),
    ]

    assert select_release_asset(assets, "2.0.5", platform_name="Darwin") is None


def test_select_release_asset_requires_exact_release_version():
    assets = [
        _asset("TunnelForge-Setup-2.0.4.exe", "old", 4),
        _asset("TunnelForge-Setup-2.0.5.exe", "current", 5),
    ]

    asset = select_release_asset(assets, "2.0.5", platform_name="Windows")

    assert asset.url == "current"


def test_select_release_asset_requires_valid_sha256_digest():
    digest = "0123456789abcdef" * 4
    assets = [
        _asset("TunnelForge-Setup-2.0.5.exe", "setup", 4, f"sha256:{digest.upper()}"),
    ]

    asset = select_release_asset(assets, "2.0.5", platform_name="Windows")

    assert asset.sha256 == digest


@pytest.mark.parametrize(
    "digest",
    [
        None,
        "sha512:" + "a" * 64,
        "sha256:" + "a" * 63,
        "sha256:" + "g" * 64,
    ],
)
def test_select_release_asset_rejects_missing_or_malformed_digest(digest):
    assets = [
        _asset("TunnelForge-Setup-2.0.5.exe", "setup", 4, digest),
    ]

    with pytest.raises(DownloadError, match="SHA-256"):
        select_release_asset(assets, "2.0.5", platform_name="Windows")


def test_download_installer_uses_configurable_timeout(monkeypatch, tmp_path):
    """download_installer가 하드코딩된 30초 대신 self.timeout을 전달해야 한다 (CC-048 회귀)"""
    downloader = UpdateDownloader()
    downloader.download_url = "https://example.com/TunnelForge-Setup-2.0.5.exe"
    downloader.file_size = 10
    downloader.expected_sha256 = hashlib.sha256(b"0123456789").hexdigest()

    config_manager = MagicMock()
    config_manager.get_network_timeout_download.return_value = 77
    downloader._config_manager = config_manager

    mock_response = MagicMock()
    mock_response.headers = {'content-length': '10'}
    mock_response.iter_content.return_value = [b'0123456789']
    monkeypatch.setattr("src.core.update_downloader.tempfile.mkdtemp", lambda **_kwargs: str(tmp_path))

    with patch('src.core.update_downloader.requests.get', return_value=mock_response) as mock_get:
        downloader.download_installer()

    assert mock_get.call_args.kwargs['timeout'] == 77


def test_download_installer_rejects_digest_mismatch_and_removes_partial_file(
    monkeypatch, tmp_path
):
    update_dir = tmp_path / "tunnelforge-update-test"
    update_dir.mkdir()
    downloader = UpdateDownloader()
    downloader.download_url = "https://example.com/TunnelForge-Setup-2.0.5.exe"
    downloader.file_size = 4
    downloader.expected_sha256 = hashlib.sha256(b"evil").hexdigest()

    response = MagicMock()
    response.headers = {"content-length": "4"}
    response.iter_content.return_value = [b"safe"]
    monkeypatch.setattr(
        "src.core.update_downloader.tempfile.mkdtemp",
        lambda **_kwargs: str(update_dir),
    )
    monkeypatch.setattr("src.core.update_downloader.requests.get", lambda *args, **kwargs: response)

    with pytest.raises(DownloadError, match="SHA-256"):
        downloader.download_installer()

    final_path = update_dir / "TunnelForge-Setup-2.0.5.exe"
    assert not final_path.exists()
    assert not Path(f"{final_path}.part").exists()
    assert not update_dir.exists()


def test_download_installer_cleans_temp_dir_when_cancelled_after_last_chunk(
    monkeypatch, tmp_path
):
    update_dir = tmp_path / "tunnelforge-update-cancelled"
    update_dir.mkdir()
    downloader = UpdateDownloader()
    downloader.download_url = "https://example.com/TunnelForge-Setup-2.0.5.exe"
    downloader.file_size = 4
    downloader.expected_sha256 = hashlib.sha256(b"safe").hexdigest()

    def chunks():
        yield b"safe"
        downloader.cancel()

    response = MagicMock()
    response.headers = {"content-length": "4"}
    response.iter_content.return_value = chunks()
    monkeypatch.setattr(
        "src.core.update_downloader.tempfile.mkdtemp",
        lambda **_kwargs: str(update_dir),
    )
    monkeypatch.setattr(
        "src.core.update_downloader.requests.get",
        lambda *args, **kwargs: response,
    )

    with pytest.raises(DownloadError, match="취소"):
        downloader.download_installer()

    assert not update_dir.exists()


def test_download_installer_cleans_final_file_when_cancelled_after_replace(
    monkeypatch, tmp_path
):
    update_dir = tmp_path / "tunnelforge-update-replaced"
    update_dir.mkdir()
    downloader = UpdateDownloader()
    downloader.download_url = "https://example.com/TunnelForge-Setup-2.0.5.exe"
    downloader.file_size = 4
    downloader.expected_sha256 = hashlib.sha256(b"safe").hexdigest()

    response = MagicMock()
    response.headers = {"content-length": "4"}
    response.iter_content.return_value = [b"safe"]
    real_replace = os.replace

    def replace_then_cancel(source, destination):
        real_replace(source, destination)
        downloader.cancel()

    monkeypatch.setattr(
        "src.core.update_downloader.tempfile.mkdtemp",
        lambda **_kwargs: str(update_dir),
    )
    monkeypatch.setattr(
        "src.core.update_downloader.requests.get",
        lambda *args, **kwargs: response,
    )
    monkeypatch.setattr("src.core.update_downloader.os.replace", replace_then_cancel)

    with pytest.raises(DownloadError, match="취소"):
        downloader.download_installer()

    assert not update_dir.exists()


def test_download_installer_removes_partial_file_on_unexpected_exception(
    monkeypatch, tmp_path
):
    downloader = UpdateDownloader()
    downloader.download_url = "https://example.com/TunnelForge-Setup-2.0.5.exe"
    downloader.file_size = 4
    downloader.expected_sha256 = hashlib.sha256(b"safe").hexdigest()

    response = MagicMock()
    response.headers = {"content-length": "4"}
    response.iter_content.return_value = [b"safe"]
    monkeypatch.setattr(
        "src.core.update_downloader.tempfile.mkdtemp",
        lambda **_kwargs: str(tmp_path),
    )
    monkeypatch.setattr(
        "src.core.update_downloader.requests.get",
        lambda *args, **kwargs: response,
    )

    def fail_progress(*_args):
        raise RuntimeError("callback failed")

    with pytest.raises(DownloadError, match="callback failed"):
        downloader.download_installer(progress_callback=fail_progress)

    final_path = tmp_path / "TunnelForge-Setup-2.0.5.exe"
    assert not final_path.exists()
    assert not Path(f"{final_path}.part").exists()


def test_get_installer_info_does_not_reuse_stale_asset_metadata(monkeypatch):
    downloader = UpdateDownloader()
    downloader.download_url = "https://example.com/stale.exe"
    downloader.file_size = 4
    downloader.expected_sha256 = "a" * 64

    response = MagicMock()
    response.json.return_value = {"tag_name": "v2.0.5", "assets": []}
    monkeypatch.setattr(
        "src.core.update_downloader.requests.get",
        lambda *args, **kwargs: response,
    )

    with pytest.raises(DownloadError, match="설치 파일을 찾을 수 없습니다"):
        downloader.get_installer_info()

    assert downloader.download_url is None
    assert downloader.expected_sha256 is None


def test_update_worker_emits_verification_metadata_before_download():
    worker = UpdateDownloadWorker()
    worker.downloader = MagicMock()
    worker.downloader.get_installer_info.return_value = (
        "2.0.5",
        "https://example.com/TunnelForge-Setup-2.0.5.exe",
        4,
    )
    worker.downloader.expected_sha256 = "a" * 64
    worker.downloader.download_installer.return_value = "installer.exe"
    events = []
    worker.verification_ready.connect(
        lambda sha256, size: events.append(("verification", sha256, size))
    )
    worker.info_fetched.connect(
        lambda version, size: events.append(("info", version, size))
    )

    worker.run()

    assert events[:2] == [
        ("verification", "a" * 64, 4),
        ("info", "2.0.5", 4),
    ]


def test_update_worker_discards_returned_installer_when_cancelled_before_success():
    worker = UpdateDownloadWorker()
    worker.downloader = MagicMock()
    worker.downloader.get_installer_info.return_value = (
        "2.0.5",
        "https://example.com/TunnelForge-Setup-2.0.5.exe",
        4,
    )
    worker.downloader.expected_sha256 = "a" * 64

    def cancel_before_return(**_kwargs):
        worker.cancel()
        return "installer.exe"

    worker.downloader.download_installer.side_effect = cancel_before_return
    finished = []
    worker.finished.connect(lambda success, result: finished.append((success, result)))

    worker.run()

    worker.downloader.discard_downloaded_installer.assert_called_once_with(
        "installer.exe"
    )
    assert finished == []


def test_discard_downloaded_installer_ignores_unowned_path(tmp_path):
    downloader = UpdateDownloader()
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    outside_file = outside_dir / "installer.exe"
    outside_file.write_bytes(b"outside")

    downloader.discard_downloaded_installer(str(outside_file))

    assert outside_file.exists()
    assert outside_dir.exists()


def test_discard_downloaded_installer_removes_owned_success_path(
    monkeypatch, tmp_path
):
    update_dir = tmp_path / "tunnelforge-update-success"
    update_dir.mkdir()
    downloader = UpdateDownloader()
    downloader.download_url = "https://example.com/TunnelForge-Setup-2.0.5.exe"
    downloader.file_size = 4
    downloader.expected_sha256 = hashlib.sha256(b"safe").hexdigest()

    response = MagicMock()
    response.headers = {"content-length": "4"}
    response.iter_content.return_value = [b"safe"]
    monkeypatch.setattr(
        "src.core.update_downloader.tempfile.mkdtemp",
        lambda **_kwargs: str(update_dir),
    )
    monkeypatch.setattr(
        "src.core.update_downloader.requests.get",
        lambda *args, **kwargs: response,
    )

    installer_path = downloader.download_installer()

    assert Path(installer_path).exists()
    assert update_dir.exists()

    downloader.discard_downloaded_installer(installer_path)

    assert not update_dir.exists()


def test_discard_downloaded_installer_rejects_unowned_sibling(
    monkeypatch, tmp_path
):
    update_dir = tmp_path / "tunnelforge-update-sibling"
    update_dir.mkdir()
    downloader = UpdateDownloader()
    downloader.download_url = "https://example.com/TunnelForge-Setup-2.0.5.exe"
    downloader.installer_filename = "TunnelForge-Setup-2.0.5.exe"
    downloader.file_size = 4
    downloader.expected_sha256 = hashlib.sha256(b"safe").hexdigest()
    response = MagicMock()
    response.headers = {"content-length": "4"}
    response.iter_content.return_value = [b"safe"]
    monkeypatch.setattr(
        "src.core.update_downloader.tempfile.mkdtemp",
        lambda **_kwargs: str(update_dir),
    )
    monkeypatch.setattr(
        "src.core.update_downloader.requests.get",
        lambda *_args, **_kwargs: response,
    )

    downloader.download_installer()
    sibling = update_dir / "unowned.exe"
    sibling.write_bytes(b"victim")

    assert downloader.discard_downloaded_installer(str(sibling)) is False
    assert sibling.read_bytes() == b"victim"


def test_discard_downloaded_installer_rejects_replaced_owned_parent(
    monkeypatch, tmp_path
):
    update_dir = tmp_path / "tunnelforge-update-owned"
    moved_dir = tmp_path / "tunnelforge-update-moved"
    update_dir.mkdir()
    downloader = UpdateDownloader()
    downloader.download_url = "https://example.com/TunnelForge-Setup-2.0.5.exe"
    downloader.file_size = 4
    downloader.expected_sha256 = hashlib.sha256(b"safe").hexdigest()

    response = MagicMock()
    response.headers = {"content-length": "4"}
    response.iter_content.return_value = [b"safe"]
    monkeypatch.setattr(
        "src.core.update_downloader.tempfile.mkdtemp",
        lambda **_kwargs: str(update_dir),
    )
    monkeypatch.setattr(
        "src.core.update_downloader.requests.get",
        lambda *_args, **_kwargs: response,
    )

    installer_path = downloader.download_installer()

    try:
        os.replace(update_dir, moved_dir)
    except PermissionError:
        # Windows may prevent the swap while a secure directory token is open.
        assert Path(installer_path).read_bytes() == b"safe"
        return

    update_dir.mkdir()
    replacement = Path(installer_path)
    replacement.write_bytes(b"victim")

    downloader.discard_downloaded_installer(installer_path)

    assert replacement.read_bytes() == b"victim"
    assert (moved_dir / replacement.name).read_bytes() == b"safe"


def test_verified_dispatch_rejects_replacement_after_hash(
    monkeypatch, tmp_path
):
    update_dir = tmp_path / "tunnelforge-update-dispatch"
    update_dir.mkdir()
    downloader = UpdateDownloader()
    downloader.download_url = "https://example.com/TunnelForge-Setup-2.0.5.exe"
    downloader.file_size = 4
    downloader.expected_sha256 = hashlib.sha256(b"safe").hexdigest()

    response = MagicMock()
    response.headers = {"content-length": "4"}
    response.iter_content.return_value = [b"safe"]
    monkeypatch.setattr(
        "src.core.update_downloader.tempfile.mkdtemp",
        lambda **_kwargs: str(update_dir),
    )
    monkeypatch.setattr(
        "src.core.update_downloader.requests.get",
        lambda *_args, **_kwargs: response,
    )

    installer_path = downloader.download_installer()
    verified_type = getattr(update_integrity, "VerifiedLaunchFile")
    original_assert = verified_type._assert_dispatch_identity
    replacement_blocked = []

    def replace_before_identity_check(verified):
        replacement = update_dir / "replacement.exe"
        replacement.write_bytes(b"safe")
        try:
            os.replace(replacement, installer_path)
        except PermissionError:
            replacement_blocked.append(True)
        return original_assert(verified)

    monkeypatch.setattr(
        verified_type,
        "_assert_dispatch_identity",
        replace_before_identity_check,
    )
    dispatched = []

    with downloader.open_verified_installer(installer_path) as verified:
        if os.name == "nt":
            verified.dispatch(lambda path: dispatched.append(Path(path).read_bytes()))
            assert replacement_blocked == [True]
            assert dispatched == [b"safe"]
        else:
            with pytest.raises(update_integrity.IntegrityError, match="identity"):
                verified.dispatch(lambda path: dispatched.append(path))
            assert dispatched == []


def test_release_asset_rejects_size_above_shared_installer_limit():
    assets = [
        _asset(
            "TunnelForge-Setup-2.0.5.exe",
            "setup",
            update_integrity.MAX_INSTALLER_SIZE + 1,
        )
    ]

    with pytest.raises(DownloadError, match="maximum"):
        select_release_asset(assets, "2.0.5", platform_name="Windows")


def test_download_rejects_valid_content_length_mismatch_before_body(
    monkeypatch, tmp_path
):
    update_dir = tmp_path / "tunnelforge-update-length"
    update_dir.mkdir()
    downloader = UpdateDownloader()
    downloader.download_url = "https://example.com/TunnelForge-Setup-2.0.5.exe"
    downloader.file_size = 4
    downloader.expected_sha256 = hashlib.sha256(b"safe").hexdigest()

    response = MagicMock()
    response.headers = {"content-length": "5"}
    response.iter_content.side_effect = AssertionError("response body was read")
    monkeypatch.setattr(
        "src.core.update_downloader.tempfile.mkdtemp",
        lambda **_kwargs: str(update_dir),
    )
    monkeypatch.setattr(
        "src.core.update_downloader.requests.get",
        lambda *_args, **_kwargs: response,
    )

    with pytest.raises(DownloadError, match="Content-Length"):
        downloader.download_installer()

    response.iter_content.assert_not_called()


@pytest.mark.parametrize(
    "content_length",
    [None, "not-a-number", "+5", "-0", " 5", "5 ", "\u0665", "5x"],
)
def test_download_streams_safely_without_valid_content_length(
    monkeypatch, tmp_path, content_length
):
    update_dir = tmp_path / f"tunnelforge-update-{content_length!r}"
    update_dir.mkdir()
    downloader = UpdateDownloader()
    downloader.download_url = "https://example.com/TunnelForge-Setup-2.0.5.exe"
    downloader.file_size = 4
    downloader.expected_sha256 = hashlib.sha256(b"safe").hexdigest()

    response = MagicMock()
    response.headers = (
        {} if content_length is None else {"content-length": content_length}
    )
    response.iter_content.return_value = [b"sa", b"fe"]
    monkeypatch.setattr(
        "src.core.update_downloader.tempfile.mkdtemp",
        lambda **_kwargs: str(update_dir),
    )
    monkeypatch.setattr(
        "src.core.update_downloader.requests.get",
        lambda *_args, **_kwargs: response,
    )

    installer_path = downloader.download_installer()

    assert Path(installer_path).read_bytes() == b"safe"


def test_download_rejects_oversized_chunk_before_write(
    monkeypatch, tmp_path
):
    update_dir = tmp_path / "tunnelforge-update-oversized"
    update_dir.mkdir()
    part_path = update_dir / "TunnelForge-Setup-2.0.5.exe.part"
    part_path.write_bytes(b"")
    downloader = UpdateDownloader()
    downloader.download_url = "https://example.com/TunnelForge-Setup-2.0.5.exe"
    downloader.file_size = 4
    downloader.expected_sha256 = hashlib.sha256(b"safe").hexdigest()

    response = MagicMock()
    response.headers = {}
    response.iter_content.return_value = [b"safe!"]
    monkeypatch.setattr(
        "src.core.update_downloader.tempfile.mkdtemp",
        lambda **_kwargs: str(update_dir),
    )
    monkeypatch.setattr(
        "src.core.update_downloader.requests.get",
        lambda *_args, **_kwargs: response,
    )
    file_handle = MagicMock()
    file_handle.__enter__.return_value = file_handle
    open_mock = MagicMock(return_value=file_handle)
    monkeypatch.setattr(
        "src.core.update_downloader.open",
        open_mock,
        raising=False,
    )
    progress = MagicMock()

    with pytest.raises(DownloadError, match="exceeds expected size"):
        downloader.download_installer(progress_callback=progress)

    progress.assert_not_called()
    file_handle.write.assert_not_called()
    assert not (update_dir / "TunnelForge-Setup-2.0.5.exe").exists()
    assert not part_path.exists()
    assert not update_dir.exists()


def test_content_length_parser_accepts_ascii_digits_only():
    parser = getattr(update_integrity, "parse_content_length", None)

    assert callable(parser)
    assert parser("004") == 4
    assert parser("0") == 0
    for raw in (None, "+4", "-0", " 4", "4 ", "\u0664", "4x"):
        assert parser(raw) is None
