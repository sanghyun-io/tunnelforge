import hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from bootstrapper import bootstrapper as bundled_bootstrapper
from bootstrapper import downloader as modular_downloader


VALID_DIGEST = "sha256:" + "a" * 64
OFFLINE_NAME = "TunnelForge-Setup-2.3.1.exe"
DOWNLOADER_IMPLEMENTATIONS = [
    (modular_downloader, modular_downloader.InstallerDownloader),
    (bundled_bootstrapper, bundled_bootstrapper.InstallerDownloader),
]


def _asset(name, url, size, digest=VALID_DIGEST):
    return {
        "name": name,
        "browser_download_url": url,
        "size": size,
        "digest": digest,
    }


def _release_response(assets):
    response = MagicMock()
    response.json.return_value = {"tag_name": "v2.3.1", "assets": assets}
    return response


def _configure_download(downloader, expected_bytes, expected_size=None):
    downloader.download_url = f"https://example.com/{OFFLINE_NAME}"
    downloader.file_size = expected_size or len(expected_bytes)
    downloader.expected_sha256 = hashlib.sha256(expected_bytes).hexdigest()
    downloader.installer_filename = OFFLINE_NAME


@pytest.mark.parametrize(
    ("module", "downloader_class", "digest"),
    [
        (*implementation, digest)
        for implementation in DOWNLOADER_IMPLEMENTATIONS
        for digest in (None, "sha256:bad")
    ],
)
def test_bootstrapper_missing_digest_fails_before_download(
    monkeypatch, module, downloader_class, digest
):
    downloader = downloader_class()
    api_response = _release_response([_asset(OFFLINE_NAME, "offline", 4, digest)])
    calls = []

    def get(url, **kwargs):
        calls.append((url, kwargs))
        if kwargs.get("stream"):
            pytest.fail("download stream opened without valid digest metadata")
        return api_response

    monkeypatch.setattr(module.requests, "get", get)

    with pytest.raises(module.DownloadError, match="SHA-256"):
        downloader.get_latest_release()

    assert len(calls) == 1
    assert downloader.download_url is None


@pytest.mark.parametrize(
    ("module", "downloader_class"),
    DOWNLOADER_IMPLEMENTATIONS,
)
def test_bootstrapper_does_not_select_websetup_as_offline_installer(
    monkeypatch, module, downloader_class
):
    downloader = downloader_class()
    response = _release_response(
        [
            _asset("TunnelForge-WebSetup.exe", "web", 1),
            _asset(OFFLINE_NAME, "offline", 4),
        ]
    )
    monkeypatch.setattr(module.requests, "get", lambda *_args, **_kwargs: response)

    version, url, size = downloader.get_latest_release()

    assert version == "2.3.1"
    assert url == "offline"
    assert size == 4
    assert downloader.download_url == "offline"
    assert downloader.expected_sha256 == "a" * 64


@pytest.mark.parametrize(
    ("module", "downloader_class"),
    DOWNLOADER_IMPLEMENTATIONS,
)
def test_bootstrapper_digest_mismatch_removes_partial_output(
    monkeypatch, tmp_path, module, downloader_class
):
    download_dir = tmp_path / "download"
    download_dir.mkdir()
    downloader = downloader_class()
    _configure_download(downloader, b"evil")

    response = MagicMock()
    response.headers = {"content-length": "4"}
    response.iter_content.return_value = [b"safe"]
    monkeypatch.setattr(module.requests, "get", lambda *_args, **_kwargs: response)
    monkeypatch.setattr(module.tempfile, "mkdtemp", lambda **_kwargs: str(download_dir))

    with pytest.raises(module.DownloadError, match="SHA-256"):
        downloader.download_installer()

    final_path = download_dir / OFFLINE_NAME
    assert not final_path.exists()
    assert not Path(f"{final_path}.part").exists()


@pytest.mark.parametrize(
    ("module", "downloader_class"),
    DOWNLOADER_IMPLEMENTATIONS,
)
def test_bootstrapper_size_mismatch_removes_all_owned_download_files(
    monkeypatch, tmp_path, module, downloader_class
):
    download_dir = tmp_path / "download"
    download_dir.mkdir()
    downloader = downloader_class()
    _configure_download(downloader, b"safe", expected_size=5)

    response = MagicMock()
    response.headers = {"content-length": "4"}
    response.iter_content.return_value = [b"safe"]
    monkeypatch.setattr(module.requests, "get", lambda *_args, **_kwargs: response)
    monkeypatch.setattr(module.tempfile, "mkdtemp", lambda **_kwargs: str(download_dir))

    with pytest.raises(module.DownloadError, match="size"):
        downloader.download_installer()

    final_path = download_dir / OFFLINE_NAME
    assert not final_path.exists()
    assert not Path(f"{final_path}.part").exists()
    assert not download_dir.exists()


@pytest.mark.parametrize(
    ("module", "downloader_class"),
    DOWNLOADER_IMPLEMENTATIONS,
)
def test_bootstrapper_streaming_cancel_removes_all_owned_download_files(
    monkeypatch, tmp_path, module, downloader_class
):
    download_dir = tmp_path / "download"
    download_dir.mkdir()
    downloader = downloader_class()
    _configure_download(downloader, b"safe")

    def chunks():
        yield b"sa"
        downloader.cancel()
        yield b"fe"

    response = MagicMock()
    response.headers = {"content-length": "4"}
    response.iter_content.return_value = chunks()
    monkeypatch.setattr(module.requests, "get", lambda *_args, **_kwargs: response)
    monkeypatch.setattr(module.tempfile, "mkdtemp", lambda **_kwargs: str(download_dir))

    with pytest.raises(module.DownloadError, match="취소"):
        downloader.download_installer()

    final_path = download_dir / OFFLINE_NAME
    assert not final_path.exists()
    assert not Path(f"{final_path}.part").exists()
    assert not download_dir.exists()


def test_bootstrapper_launch_rejects_tampered_installer(monkeypatch, tmp_path):
    installer = tmp_path / OFFLINE_NAME
    installer.write_bytes(b"tampered")
    app = bundled_bootstrapper.BootstrapperApp.__new__(
        bundled_bootstrapper.BootstrapperApp
    )
    app.downloaded_file = str(installer)
    app.downloader = SimpleNamespace(
        expected_sha256=hashlib.sha256(b"expected").hexdigest(),
        file_size=len(b"expected"),
    )
    errors = []
    app._show_error = errors.append
    popen = MagicMock()
    monkeypatch.setattr(bundled_bootstrapper.subprocess, "Popen", popen)

    app._launch_installer()

    assert not installer.exists()
    assert errors
    popen.assert_not_called()


def test_bootstrapper_keeps_verified_installer_when_launch_fails(monkeypatch, tmp_path):
    installer = tmp_path / OFFLINE_NAME
    installer.write_bytes(b"expected")
    app = bundled_bootstrapper.BootstrapperApp.__new__(
        bundled_bootstrapper.BootstrapperApp
    )
    app.downloaded_file = str(installer)
    app.downloader = SimpleNamespace(
        expected_sha256=hashlib.sha256(b"expected").hexdigest(),
        file_size=len(b"expected"),
    )
    errors = []
    app._show_error = errors.append
    monkeypatch.setattr(bundled_bootstrapper.subprocess, "DETACHED_PROCESS", 0)
    monkeypatch.setattr(bundled_bootstrapper.subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    monkeypatch.setattr(
        bundled_bootstrapper.subprocess,
        "Popen",
        MagicMock(side_effect=OSError("launch failed")),
    )

    app._launch_installer()

    assert installer.exists()
    assert len(errors) == 1
    assert errors[0].startswith("설치 프로그램 실행 실패")
