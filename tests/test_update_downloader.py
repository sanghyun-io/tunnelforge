import hashlib
import os
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from src import update_integrity
from src.core.update_downloader import DownloadError, UpdateDownloader, select_release_asset
from src.ui.workers.update_worker import UpdateDownloadWorker


VALID_DIGEST = "sha256:" + "a" * 64


def _assert_failed_download_cleanup(temp_dir, final_path, *, part_created=True):
    part_path = Path(f"{final_path}.part")
    assert not final_path.exists()
    if os.name == "nt":
        assert not part_path.exists()
        assert not temp_dir.exists()
    else:
        assert temp_dir.exists()
        if part_created:
            assert part_path.exists()


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


@pytest.mark.parametrize(
    ("platform_name", "release_version"),
    [("Windows", "2.0:5"), ("Darwin", "2.0/5")],
)
def test_select_release_asset_rejects_unsafe_version_leaf(
    platform_name, release_version
):
    with pytest.raises(DownloadError, match="filename"):
        select_release_asset([], release_version, platform_name=platform_name)


@pytest.mark.parametrize(
    "device_stem",
    [
        "COM\u00b9",
        "com\u00b2",
        "Com\u00b3",
        "LPT\u00b9",
        "lpt\u00b2",
        "Lpt\u00b3",
    ],
)
def test_select_release_asset_rejects_superscript_reserved_device_stem(
    monkeypatch, device_stem
):
    monkeypatch.setattr(
        "src.core.update_downloader.WINDOWS_INSTALLER_FILENAME_PREFIX", ""
    )

    with pytest.raises(DownloadError, match="filename"):
        select_release_asset([], device_stem, platform_name="Windows")


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
    _assert_failed_download_cleanup(update_dir, final_path)


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

    _assert_failed_download_cleanup(
        update_dir,
        update_dir / "TunnelForge-Setup-2.0.5.exe",
    )


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
    real_publish = update_integrity.publish_owned_temp_file

    def publish_then_cancel(owner, source, destination):
        result = real_publish(owner, source, destination)
        downloader.cancel()
        return result

    monkeypatch.setattr(
        "src.core.update_downloader.tempfile.mkdtemp",
        lambda **_kwargs: str(update_dir),
    )
    monkeypatch.setattr(
        "src.core.update_downloader.requests.get",
        lambda *args, **kwargs: response,
    )
    monkeypatch.setattr(
        "src.core.update_downloader.publish_owned_temp_file", publish_then_cancel
    )

    with pytest.raises(DownloadError, match="취소"):
        downloader.download_installer()

    if os.name == "nt":
        assert not update_dir.exists()
    else:
        assert update_dir.exists()
        assert (update_dir / "TunnelForge-Setup-2.0.5.exe").exists()
        assert (update_dir / "TunnelForge-Setup-2.0.5.exe.part").exists()


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
    _assert_failed_download_cleanup(tmp_path, final_path)


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

    removed = downloader.discard_downloaded_installer(installer_path)

    if os.name == "nt":
        assert removed is True
        assert not Path(installer_path).exists()
        assert not update_dir.exists()
    else:
        assert removed is False
        assert Path(installer_path).exists()
        assert update_dir.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX-only cleanup capability")
def test_posix_owned_temp_directory_never_unlinks_or_removes_root(tmp_path):
    owner = update_integrity.OwnedTempDirectory(tmp_path)
    installer = tmp_path / "installer.exe"
    installer.write_bytes(b"safe")

    assert owner.claim_files((installer,)) is True
    assert owner.discard_files((installer,)) is False
    assert installer.read_bytes() == b"safe"
    assert owner.remove_if_empty() is False
    assert tmp_path.exists()


def test_owned_temp_directory_create_file_preserves_existing_child(tmp_path):
    owner = update_integrity.OwnedTempDirectory(tmp_path)
    victim = tmp_path / "installer.exe.part"
    victim.write_bytes(b"victim")

    with pytest.raises(OSError):
        owner.create_file(victim.name)

    assert victim.read_bytes() == b"victim"


@pytest.mark.parametrize(
    "name",
    [
        "",
        ".",
        "..",
        "nested/file.part",
        "nested\\file.part",
        "bad\x00.part",
        "bad\x1f.part",
    ],
)
def test_owned_temp_directory_create_file_requires_exact_basename(tmp_path, name):
    owner = update_integrity.OwnedTempDirectory(tmp_path)

    with pytest.raises(update_integrity.IntegrityError, match="basename"):
        owner.create_file(name)


@pytest.mark.skipif(os.name != "nt", reason="Windows filename behavior")
@pytest.mark.parametrize(
    "name",
    [
        "bad:name.part",
        "bad<name.part",
        "bad>name.part",
        'bad"name.part',
        "bad|name.part",
        "bad?name.part",
        "bad*name.part",
        "trailing.part.",
        "trailing.part ",
        "CON",
        "con.txt",
        "PRN.part",
        "AUX.exe",
        "NUL.part",
        "COM1.part",
        "com9.exe",
        "COM\u00b9.part",
        "com\u00b2.exe",
        "Com\u00b3",
        "LPT1.part",
        "lpt9.exe",
        "LPT\u00b9.part",
        "lpt\u00b2.exe",
        "Lpt\u00b3",
    ],
)
def test_windows_create_file_rejects_invalid_leaf_before_create(
    monkeypatch, tmp_path, name
):
    owner = update_integrity.OwnedTempDirectory(tmp_path)
    identity_matches = MagicMock(side_effect=AssertionError("validated too late"))
    monkeypatch.setattr(owner, "identity_matches", identity_matches)
    create_file = MagicMock()
    monkeypatch.setattr(update_integrity, "_CreateFileW", create_file)

    with pytest.raises(update_integrity.IntegrityError, match="basename"):
        owner.create_file(name)

    identity_matches.assert_not_called()
    create_file.assert_not_called()


@pytest.mark.skipif(os.name != "nt", reason="Windows alternate data streams")
def test_windows_create_file_rejects_ads_without_touching_victim(tmp_path):
    owner = update_integrity.OwnedTempDirectory(tmp_path)
    victim = tmp_path / "victim"
    stream = tmp_path / "victim:existing"
    victim.write_bytes(b"victim-content")
    stream.write_bytes(b"victim-stream")

    with pytest.raises(update_integrity.IntegrityError, match="basename"):
        owner.create_file("victim:installer.part")

    assert victim.read_bytes() == b"victim-content"
    assert stream.read_bytes() == b"victim-stream"


def test_owned_temp_directory_create_file_accepts_release_filename(tmp_path):
    owner = update_integrity.OwnedTempDirectory(tmp_path)

    with owner.create_file("TunnelForge-Setup-2.3.1.exe.part") as destination:
        destination.write(b"safe")

    assert (tmp_path / "TunnelForge-Setup-2.3.1.exe.part").read_bytes() == b"safe"


def test_owned_temp_directory_create_file_rejects_replaced_parent(tmp_path):
    parent = tmp_path / "download"
    moved = tmp_path / "moved"
    parent.mkdir()
    owner = update_integrity.OwnedTempDirectory(parent)
    os.rename(parent, moved)
    parent.mkdir()
    victim = parent / "installer.exe.part"
    victim.write_bytes(b"victim")

    with pytest.raises(update_integrity.IntegrityError, match="parent identity"):
        owner.create_file(victim.name)

    assert victim.read_bytes() == b"victim"


def test_update_downloader_preserves_preexisting_part_file(monkeypatch, tmp_path):
    update_dir = tmp_path / "download"
    update_dir.mkdir()
    downloader = UpdateDownloader()
    downloader.download_url = "https://example.com/installer.exe"
    downloader.file_size = 4
    downloader.expected_sha256 = hashlib.sha256(b"safe").hexdigest()
    downloader.installer_filename = "installer.exe"
    victim = update_dir / "installer.exe.part"
    victim.write_bytes(b"victim")
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

    with pytest.raises(DownloadError):
        downloader.download_installer()

    assert victim.read_bytes() == b"victim"
    response.iter_content.assert_not_called()


def test_update_downloader_preserves_race_created_part_file(monkeypatch, tmp_path):
    update_dir = tmp_path / "download"
    update_dir.mkdir()
    downloader = UpdateDownloader()
    downloader.download_url = "https://example.com/installer.exe"
    downloader.file_size = 4
    downloader.expected_sha256 = hashlib.sha256(b"safe").hexdigest()
    downloader.installer_filename = "installer.exe"
    victim = update_dir / "installer.exe.part"
    response = MagicMock()
    response.headers = {"content-length": "4"}
    response.iter_content.return_value = [b"safe"]

    def race_create(*_args, **_kwargs):
        victim.write_bytes(b"victim")
        return response

    monkeypatch.setattr(
        "src.core.update_downloader.tempfile.mkdtemp",
        lambda **_kwargs: str(update_dir),
    )
    monkeypatch.setattr("src.core.update_downloader.requests.get", race_create)

    with pytest.raises(DownloadError):
        downloader.download_installer()

    assert victim.read_bytes() == b"victim"
    response.iter_content.assert_not_called()


@pytest.mark.skipif(os.name != "nt", reason="Windows handle behavior")
def test_windows_create_file_uses_exclusive_no_follow_handle_and_closes_fd(
    monkeypatch, tmp_path
):
    owner = update_integrity.OwnedTempDirectory(tmp_path)
    assert owner.retain() is True
    create_file = MagicMock(return_value=71)
    close_descriptor = MagicMock()
    close_handle = MagicMock()
    monkeypatch.setattr(owner, "identity_matches", lambda: True)
    monkeypatch.setattr(update_integrity, "_CreateFileW", create_file)
    monkeypatch.setattr(
        update_integrity,
        "_windows_handle_information",
        lambda _handle: ((1, 2), 0),
    )
    monkeypatch.setattr(update_integrity.msvcrt, "open_osfhandle", lambda *_args: 72)
    monkeypatch.setattr(
        update_integrity.os,
        "fdopen",
        MagicMock(side_effect=OSError("fdopen failed")),
    )
    monkeypatch.setattr(update_integrity.os, "close", close_descriptor)
    monkeypatch.setattr(update_integrity, "_windows_close_handle_safely", close_handle)

    with pytest.raises(OSError, match="fdopen failed"):
        owner.create_file("installer.exe.part")

    args = create_file.call_args.args
    assert args[1] == update_integrity._GENERIC_READ | update_integrity._GENERIC_WRITE
    assert args[2] == update_integrity._FILE_SHARE_READ
    assert args[4] == update_integrity._CREATE_NEW
    assert args[5] == update_integrity._FILE_FLAG_OPEN_REPARSE_POINT
    close_descriptor.assert_called_once_with(72)
    close_handle.assert_not_called()


@pytest.mark.skipif(os.name != "nt", reason="Windows handle behavior")
def test_windows_verified_lease_closes_descriptor_when_fdopen_fails(
    monkeypatch, tmp_path
):
    installer = tmp_path / "installer.exe"
    installer.write_bytes(b"safe")
    close_descriptor = MagicMock()
    close_handle = MagicMock()
    monkeypatch.setattr(
        update_integrity, "_windows_open_handle", lambda _path, **_kwargs: 71
    )
    monkeypatch.setattr(
        update_integrity,
        "_windows_handle_information",
        lambda _handle: ((1, 2), 0),
    )
    monkeypatch.setattr(update_integrity.msvcrt, "open_osfhandle", lambda *_args: 72)
    monkeypatch.setattr(
        update_integrity.os,
        "fdopen",
        MagicMock(side_effect=OSError("fdopen failed")),
    )
    monkeypatch.setattr(update_integrity.os, "close", close_descriptor)
    monkeypatch.setattr(update_integrity, "_windows_close_handle", close_handle)

    lease = update_integrity.VerifiedFileLease(
        installer,
        hashlib.sha256(b"safe").hexdigest(),
        4,
    )
    with pytest.raises(OSError, match="fdopen failed"):
        lease._open_source()

    close_descriptor.assert_called_once_with(72)
    close_handle.assert_not_called()


@pytest.mark.skipif(os.name != "nt", reason="Windows handle behavior")
def test_windows_retain_records_registered_final_child_identity(monkeypatch, tmp_path):
    update_dir = tmp_path / "tunnelforge-update-identity"
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
    owner = next(iter(downloader._owned_temp_dirs.values()))

    assert Path(installer_path).name in getattr(owner, "_child_identities", {})


@pytest.mark.skipif(os.name != "nt", reason="Windows handle behavior")
def test_windows_delete_owned_child_rejects_identity_mismatch(monkeypatch):
    delete_child = getattr(update_integrity, "_windows_delete_owned_child", None)
    assert delete_child is not None
    mark_for_delete = MagicMock(return_value=True)
    close_handle = MagicMock()
    monkeypatch.setattr(
        update_integrity, "_windows_open_handle", lambda _path, **_kwargs: 41
    )
    monkeypatch.setattr(
        update_integrity,
        "_windows_handle_information",
        lambda _handle: ((2, 3), 0),
    )
    monkeypatch.setattr(update_integrity, "_windows_mark_for_delete", mark_for_delete)
    monkeypatch.setattr(update_integrity, "_windows_close_handle", close_handle)

    assert delete_child("C:\\temp\\installer.exe", (1, 3)) is False
    mark_for_delete.assert_not_called()
    assert call(41) in close_handle.call_args_list


@pytest.mark.skipif(os.name != "nt", reason="Windows handle behavior")
def test_windows_delete_owned_child_closes_handle_after_disposition_failure(
    monkeypatch,
):
    close_handle = MagicMock()
    monkeypatch.setattr(
        update_integrity, "_windows_open_handle", lambda _path, **_kwargs: 42
    )
    monkeypatch.setattr(
        update_integrity,
        "_windows_handle_information",
        lambda _handle: ((1, 3), 0),
    )
    monkeypatch.setattr(update_integrity, "_windows_mark_for_delete", lambda _handle: False)
    monkeypatch.setattr(update_integrity, "_windows_close_handle", close_handle)

    assert update_integrity._windows_delete_owned_child(
        "C:\\temp\\installer.exe", (1, 3)
    ) is False
    assert call(42) in close_handle.call_args_list


@pytest.mark.skipif(os.name != "nt", reason="Windows handle behavior")
def test_windows_capture_close_error_does_not_mask_integrity_error(monkeypatch, tmp_path):
    monkeypatch.setattr(
        update_integrity, "_windows_open_handle", lambda _path, **_kwargs: 51
    )
    monkeypatch.setattr(
        update_integrity,
        "_windows_handle_information",
        lambda _handle: ((1, 1), update_integrity._FILE_ATTRIBUTE_REPARSE_POINT),
    )
    monkeypatch.setattr(
        update_integrity,
        "_windows_close_handle",
        MagicMock(side_effect=OSError("close failed")),
    )

    with pytest.raises(update_integrity.IntegrityError, match="reparse"):
        update_integrity.OwnedTempDirectory(tmp_path)


@pytest.mark.skipif(os.name != "nt", reason="Windows handle behavior")
def test_windows_retain_closes_probe_handle_after_information_failure(
    monkeypatch, tmp_path
):
    owner = update_integrity.OwnedTempDirectory(tmp_path)
    close_handle = MagicMock()
    monkeypatch.setattr(
        update_integrity, "_windows_open_handle", lambda _path, **_kwargs: 61
    )
    monkeypatch.setattr(
        update_integrity,
        "_windows_handle_information",
        MagicMock(side_effect=OSError("information failed")),
    )
    monkeypatch.setattr(update_integrity, "_windows_close_handle", close_handle)

    assert owner.retain(str(tmp_path / "installer.exe")) is False
    assert call(61) in close_handle.call_args_list


@pytest.mark.skipif(os.name != "nt", reason="Windows handle behavior")
def test_windows_discard_rejects_swapped_registered_child(monkeypatch, tmp_path):
    update_dir = tmp_path / "tunnelforge-update-child-swap"
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
    owner = next(iter(downloader._owned_temp_dirs.values()))
    owner.close()
    replacement = update_dir / "replacement.exe"
    replacement.write_bytes(b"victim")
    os.replace(replacement, installer_path)

    assert downloader.discard_downloaded_installer(installer_path) is False
    assert Path(installer_path).read_bytes() == b"victim"


def test_download_publish_preserves_preexisting_final(monkeypatch, tmp_path):
    update_dir = tmp_path / "tunnelforge-update-existing-final"
    update_dir.mkdir()
    final_path = update_dir / "TunnelForge-Setup-2.0.5.exe"
    final_path.write_bytes(b"victim")
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

    with pytest.raises(DownloadError):
        downloader.download_installer()

    assert final_path.read_bytes() == b"victim"


def test_download_publish_preserves_final_injected_before_rename(monkeypatch, tmp_path):
    update_dir = tmp_path / "tunnelforge-update-injected-final"
    update_dir.mkdir()
    final_path = update_dir / "TunnelForge-Setup-2.0.5.exe"
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

    if os.name == "nt":
        publish = update_integrity._publish_windows_no_clobber

        def inject_final(part_path, published_path):
            final_path.write_bytes(b"victim")
            publish(part_path, published_path)

        monkeypatch.setattr(update_integrity, "_publish_windows_no_clobber", inject_final)
    else:
        publish = update_integrity._publish_posix_no_clobber

        def inject_final(part_path, published_path, expected_identity):
            final_path.write_bytes(b"victim")
            publish(part_path, published_path, expected_identity)

        monkeypatch.setattr(update_integrity, "_publish_posix_no_clobber", inject_final)

    with pytest.raises(DownloadError):
        downloader.download_installer()

    assert final_path.read_bytes() == b"victim"


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


def test_verified_dispatch_checks_identity_before_and_after_callback(
    monkeypatch, tmp_path
):
    installer = tmp_path / "installer.exe"
    installer.write_bytes(b"safe")
    lease = update_integrity.VerifiedFileLease(
        installer,
        hashlib.sha256(b"safe").hexdigest(),
        4,
    )
    identity_checks = []
    callback_states = []

    with lease as verified:
        original_assert = verified._assert_dispatch_identity

        def record_identity_check():
            identity_checks.append(verified.closed)
            return original_assert()

        monkeypatch.setattr(verified, "_assert_dispatch_identity", record_identity_check)

        def callback(path):
            callback_states.append((path, verified.closed))
            return "started"

        assert verified.dispatch(callback) == "started"

    assert identity_checks == [False, False]
    assert callback_states == [(str(installer.resolve()), False)]
    assert verified.closed is True


def test_verified_dispatch_does_not_return_success_after_identity_change(
    monkeypatch, tmp_path
):
    installer = tmp_path / "installer.exe"
    installer.write_bytes(b"safe")
    lease = update_integrity.VerifiedFileLease(
        installer,
        hashlib.sha256(b"safe").hexdigest(),
        4,
    )
    identity_checks = []
    callback_states = []

    with pytest.raises(update_integrity.IntegrityError, match="identity changed"):
        with lease as verified:
            def assert_identity():
                identity_checks.append(verified.closed)
                if len(identity_checks) == 2:
                    raise update_integrity.IntegrityError(
                        "release file path identity changed after dispatch"
                    )

            monkeypatch.setattr(
                verified,
                "_assert_dispatch_identity",
                assert_identity,
            )

            def callback(_path):
                callback_states.append(verified.closed)
                return "started"

            verified.dispatch(callback)

    assert identity_checks == [False, False]
    assert callback_states == [False]
    assert lease.closed is True


def test_verified_dispatch_rejects_replacement_from_callback(monkeypatch, tmp_path):
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
    owner = downloader._find_owned_parent(installer_path)
    assert owner is not None
    owner.close()
    replacement_blocked = []
    callback_calls = []

    source = None
    with downloader.open_verified_installer(installer_path) as verified:
        source = verified._source

        def callback(path):
            callback_calls.append(path)
            assert verified.closed is False
            replacement = update_dir / "replacement.exe"
            replacement.write_bytes(b"safe")
            try:
                os.replace(replacement, installer_path)
            except PermissionError:
                replacement_blocked.append(True)
            return Path(path).read_bytes()

        if os.name == "nt":
            assert verified.dispatch(callback) == b"safe"
            assert replacement_blocked == [True]
        else:
            with pytest.raises(update_integrity.IntegrityError, match="identity"):
                verified.dispatch(callback)
            assert replacement_blocked == []

        assert callback_calls == [str(Path(installer_path).resolve())]

    assert source is not None and source.closed
    assert verified.closed is True
    post_context_replacement = update_dir / "post-context-replacement.exe"
    post_context_replacement.write_bytes(b"after")
    os.replace(post_context_replacement, installer_path)
    assert Path(installer_path).read_bytes() == b"after"


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
    monkeypatch.setattr(
        update_integrity.OwnedTempDirectory,
        "create_file",
        MagicMock(return_value=file_handle),
    )
    progress = MagicMock()

    with pytest.raises(DownloadError, match="exceeds expected size"):
        downloader.download_installer(progress_callback=progress)

    progress.assert_not_called()
    file_handle.write.assert_not_called()
    _assert_failed_download_cleanup(
        update_dir,
        update_dir / "TunnelForge-Setup-2.0.5.exe",
        part_created=False,
    )


def test_content_length_parser_accepts_ascii_digits_only():
    parser = getattr(update_integrity, "parse_content_length", None)

    assert callable(parser)
    assert parser("004") == 4
    assert parser("0") == 0
    for raw in (None, "+4", "-0", " 4", "4 ", "\u0664", "4x"):
        assert parser(raw) is None
