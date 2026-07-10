import hashlib
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, call

import pytest

from bootstrapper import bootstrapper as bundled_bootstrapper
from bootstrapper import downloader as modular_downloader
from src import update_integrity


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


def _force_bundled_bootstrapper_windows(monkeypatch):
    monkeypatch.setattr(
        bundled_bootstrapper,
        "os",
        SimpleNamespace(name="nt", path=os.path),
    )


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
    _force_bundled_bootstrapper_windows(monkeypatch)
    installer = tmp_path / OFFLINE_NAME
    installer.write_bytes(b"tampered")
    app = bundled_bootstrapper.BootstrapperApp.__new__(
        bundled_bootstrapper.BootstrapperApp
    )
    app.downloaded_file = str(installer)
    discard = MagicMock(return_value=True)
    app.downloader = SimpleNamespace(
        expected_sha256=hashlib.sha256(b"expected").hexdigest(),
        file_size=len(b"expected"),
        discard_downloaded_installer=discard,
    )
    errors = []
    app._show_error = errors.append
    popen = MagicMock()
    monkeypatch.setattr(bundled_bootstrapper.subprocess, "Popen", popen)

    app._launch_installer()

    discard.assert_called_once_with(str(installer))
    assert installer.exists()
    assert errors
    popen.assert_not_called()


def test_bootstrapper_integrity_cleanup_error_does_not_mask_launch_block(
    monkeypatch, tmp_path
):
    _force_bundled_bootstrapper_windows(monkeypatch)
    installer = tmp_path / OFFLINE_NAME
    installer.write_bytes(b"tampered")
    app = bundled_bootstrapper.BootstrapperApp.__new__(
        bundled_bootstrapper.BootstrapperApp
    )
    app.downloaded_file = str(installer)
    discard = MagicMock(side_effect=OSError("cleanup failed"))
    app.downloader = SimpleNamespace(
        expected_sha256=hashlib.sha256(b"expected").hexdigest(),
        file_size=len(b"expected"),
        discard_downloaded_installer=discard,
    )
    errors = []
    app._show_error = errors.append
    popen = MagicMock()
    monkeypatch.setattr(bundled_bootstrapper.subprocess, "Popen", popen)

    app._launch_installer()

    discard.assert_called_once_with(str(installer))
    assert errors and errors[0].startswith("다운로드된 설치 파일 검증 실패")
    popen.assert_not_called()


def test_bootstrapper_keeps_verified_installer_when_launch_fails(monkeypatch, tmp_path):
    _force_bundled_bootstrapper_windows(monkeypatch)
    installer = tmp_path / OFFLINE_NAME
    installer.write_bytes(b"expected")
    app = bundled_bootstrapper.BootstrapperApp.__new__(
        bundled_bootstrapper.BootstrapperApp
    )
    app.downloaded_file = str(installer)
    downloader = bundled_bootstrapper.InstallerDownloader()
    downloader.expected_sha256 = hashlib.sha256(b"expected").hexdigest()
    downloader.file_size = len(b"expected")
    discard = MagicMock()
    monkeypatch.setattr(downloader, "discard_downloaded_installer", discard)
    leases = []
    open_verified = downloader.open_verified_installer

    def capture_lease(path):
        lease = open_verified(path)
        leases.append(lease)
        return lease

    monkeypatch.setattr(downloader, "open_verified_installer", capture_lease)
    app.downloader = downloader
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
    assert installer.read_bytes() == b"expected"
    assert len(leases) == 1 and leases[0].closed is True
    discard.assert_not_called()
    assert len(errors) == 1
    assert errors[0].startswith("설치 프로그램 실행 실패")


def test_bootstrapper_non_windows_never_launches_windows_installer(monkeypatch, tmp_path):
    installer = tmp_path / OFFLINE_NAME
    installer.write_bytes(b"verified")
    app = bundled_bootstrapper.BootstrapperApp.__new__(
        bundled_bootstrapper.BootstrapperApp
    )
    app.downloaded_file = str(installer)
    app.downloader = MagicMock()
    app.root = MagicMock()
    errors = []
    app._show_error = errors.append
    popen = MagicMock()
    monkeypatch.setattr(
        bundled_bootstrapper,
        "os",
        SimpleNamespace(name="posix", path=os.path),
    )
    monkeypatch.setattr(bundled_bootstrapper.subprocess, "Popen", popen)

    app._launch_installer()

    popen.assert_not_called()
    assert len(errors) == 1
    assert "Windows" in errors[0]
    assert ".exe" in errors[0]
    app.root.after.assert_not_called()
    app.root.destroy.assert_not_called()
    assert app.downloaded_file == str(installer)


@pytest.mark.parametrize(
    ("module", "downloader_class"),
    DOWNLOADER_IMPLEMENTATIONS,
)
def test_bootstrapper_cancel_after_metadata_is_not_reset_before_download(
    monkeypatch, module, downloader_class
):
    downloader = downloader_class()
    api_response = _release_response([_asset(OFFLINE_NAME, "offline", 4)])
    stream_calls = []

    def get(url, **kwargs):
        if kwargs.get("stream"):
            stream_calls.append(url)
            pytest.fail("cancelled task opened the download stream")
        return api_response

    monkeypatch.setattr(module.requests, "get", get)

    downloader.get_latest_release()
    downloader.cancel()

    with pytest.raises(module.DownloadError, match="취소"):
        downloader.download_installer()

    assert stream_calls == []


@pytest.mark.parametrize(
    ("module", "downloader_class"),
    DOWNLOADER_IMPLEMENTATIONS,
)
def test_bootstrapper_reset_cancellation_starts_a_new_download_task(
    monkeypatch, tmp_path, module, downloader_class
):
    downloader = downloader_class()
    api_response = _release_response(
        [
            _asset(
                OFFLINE_NAME,
                "offline",
                4,
                "sha256:" + hashlib.sha256(b"safe").hexdigest(),
            )
        ]
    )
    response = MagicMock()
    response.headers = {"content-length": "4"}
    response.iter_content.return_value = [b"safe"]

    def get(_url, **kwargs):
        return response if kwargs.get("stream") else api_response

    monkeypatch.setattr(module.requests, "get", get)
    downloader.cancel()

    assert callable(getattr(downloader, "reset_cancellation", None))
    downloader.reset_cancellation()
    downloader.get_latest_release()

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(module.tempfile, "mkdtemp", lambda **_kwargs: str(tmp_path))
        installer_path = downloader.download_installer()

    assert Path(installer_path).read_bytes() == b"safe"


def test_bootstrapper_controller_resets_cancellation_before_metadata():
    app = bundled_bootstrapper.BootstrapperApp.__new__(
        bundled_bootstrapper.BootstrapperApp
    )
    app.downloader = MagicMock()
    app.downloader.get_latest_release.side_effect = bundled_bootstrapper.DownloadError(
        "metadata failed"
    )
    app._update_status = MagicMock()
    app._show_error = MagicMock()

    app._download_worker()

    assert app.downloader.method_calls[:2] == [
        call.reset_cancellation(),
        call.get_latest_release(),
    ]


@pytest.mark.parametrize(
    ("module", "downloader_class"),
    DOWNLOADER_IMPLEMENTATIONS,
)
def test_bootstrapper_discard_ignores_unowned_path(
    tmp_path, module, downloader_class
):
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    outside_file = outside_dir / OFFLINE_NAME
    outside_file.write_bytes(b"outside")
    downloader = downloader_class()

    downloader.discard_downloaded_installer(str(outside_file))

    assert outside_file.read_bytes() == b"outside"


@pytest.mark.parametrize(
    ("module", "downloader_class"),
    DOWNLOADER_IMPLEMENTATIONS,
)
def test_bootstrapper_discard_rejects_unowned_sibling(
    monkeypatch, tmp_path, module, downloader_class
):
    download_dir = tmp_path / "download"
    download_dir.mkdir()
    downloader = downloader_class()
    downloader.download_url = "https://example.com/" + OFFLINE_NAME
    downloader.file_size = 4
    downloader.expected_sha256 = hashlib.sha256(b"safe").hexdigest()
    downloader.installer_filename = OFFLINE_NAME
    response = MagicMock()
    response.headers = {"content-length": "4"}
    response.iter_content.return_value = [b"safe"]
    monkeypatch.setattr(module.requests, "get", lambda *_args, **_kwargs: response)
    monkeypatch.setattr(module.tempfile, "mkdtemp", lambda **_kwargs: str(download_dir))

    downloader.download_installer()
    sibling = download_dir / "unowned.exe"
    sibling.write_bytes(b"victim")

    assert downloader.discard_downloaded_installer(str(sibling)) is False
    assert sibling.read_bytes() == b"victim"


@pytest.mark.parametrize(
    ("module", "downloader_class"),
    DOWNLOADER_IMPLEMENTATIONS,
)
def test_bootstrapper_publish_preserves_preexisting_final(
    monkeypatch, tmp_path, module, downloader_class
):
    download_dir = tmp_path / "download"
    download_dir.mkdir()
    final_path = download_dir / OFFLINE_NAME
    final_path.write_bytes(b"victim")
    downloader = downloader_class()
    _configure_download(downloader, b"safe")
    response = MagicMock()
    response.headers = {"content-length": "4"}
    response.iter_content.return_value = [b"safe"]
    monkeypatch.setattr(module.requests, "get", lambda *_args, **_kwargs: response)
    monkeypatch.setattr(module.tempfile, "mkdtemp", lambda **_kwargs: str(download_dir))

    with pytest.raises(module.DownloadError):
        downloader.download_installer()

    assert final_path.read_bytes() == b"victim"


@pytest.mark.parametrize(
    ("module", "downloader_class"),
    DOWNLOADER_IMPLEMENTATIONS,
)
def test_bootstrapper_publish_preserves_final_injected_before_rename(
    monkeypatch, tmp_path, module, downloader_class
):
    download_dir = tmp_path / "download"
    download_dir.mkdir()
    final_path = download_dir / OFFLINE_NAME
    downloader = downloader_class()
    _configure_download(downloader, b"safe")
    response = MagicMock()
    response.headers = {"content-length": "4"}
    response.iter_content.return_value = [b"safe"]
    monkeypatch.setattr(module.requests, "get", lambda *_args, **_kwargs: response)
    monkeypatch.setattr(module.tempfile, "mkdtemp", lambda **_kwargs: str(download_dir))

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

    with pytest.raises(module.DownloadError):
        downloader.download_installer()

    assert final_path.read_bytes() == b"victim"


@pytest.mark.parametrize(
    ("module", "downloader_class"),
    DOWNLOADER_IMPLEMENTATIONS,
)
def test_bootstrapper_discard_rejects_replaced_owned_parent(
    monkeypatch, tmp_path, module, downloader_class
):
    download_dir = tmp_path / "download"
    moved_dir = tmp_path / "moved"
    download_dir.mkdir()
    downloader = downloader_class()
    _configure_download(downloader, b"safe")

    response = MagicMock()
    response.headers = {"content-length": "4"}
    response.iter_content.return_value = [b"safe"]
    monkeypatch.setattr(module.requests, "get", lambda *_args, **_kwargs: response)
    monkeypatch.setattr(module.tempfile, "mkdtemp", lambda **_kwargs: str(download_dir))

    installer_path = downloader.download_installer()

    try:
        os.replace(download_dir, moved_dir)
    except PermissionError:
        assert Path(installer_path).read_bytes() == b"safe"
        return

    download_dir.mkdir()
    replacement = Path(installer_path)
    replacement.write_bytes(b"victim")

    downloader.discard_downloaded_installer(installer_path)

    assert replacement.read_bytes() == b"victim"
    assert (moved_dir / OFFLINE_NAME).read_bytes() == b"safe"


@pytest.mark.parametrize(
    ("module", "downloader_class"),
    DOWNLOADER_IMPLEMENTATIONS,
)
def test_bootstrapper_rejects_release_above_shared_installer_limit(
    monkeypatch, module, downloader_class
):
    downloader = downloader_class()
    response = _release_response(
        [_asset(OFFLINE_NAME, "offline", update_integrity.MAX_INSTALLER_SIZE + 1)]
    )
    monkeypatch.setattr(module.requests, "get", lambda *_args, **_kwargs: response)

    with pytest.raises(module.DownloadError, match="maximum"):
        downloader.get_latest_release()


@pytest.mark.parametrize(
    ("module", "downloader_class"),
    DOWNLOADER_IMPLEMENTATIONS,
)
def test_bootstrapper_rejects_valid_content_length_mismatch_before_body(
    monkeypatch, tmp_path, module, downloader_class
):
    download_dir = tmp_path / "download"
    download_dir.mkdir()
    downloader = downloader_class()
    _configure_download(downloader, b"safe")

    response = MagicMock()
    response.headers = {"content-length": "5"}
    response.iter_content.side_effect = AssertionError("response body was read")
    monkeypatch.setattr(module.requests, "get", lambda *_args, **_kwargs: response)
    monkeypatch.setattr(module.tempfile, "mkdtemp", lambda **_kwargs: str(download_dir))

    with pytest.raises(module.DownloadError, match="Content-Length"):
        downloader.download_installer()

    response.iter_content.assert_not_called()


@pytest.mark.parametrize(
    ("module", "downloader_class", "content_length"),
    [
        (*implementation, content_length)
        for implementation in DOWNLOADER_IMPLEMENTATIONS
        for content_length in (
            None,
            "not-a-number",
            "+5",
            "-0",
            " 5",
            "5 ",
            "\u0665",
            "5x",
        )
    ],
)
def test_bootstrapper_streams_safely_without_valid_content_length(
    monkeypatch, tmp_path, module, downloader_class, content_length
):
    download_dir = tmp_path / "download"
    download_dir.mkdir()
    downloader = downloader_class()
    _configure_download(downloader, b"safe")

    response = MagicMock()
    response.headers = (
        {} if content_length is None else {"content-length": content_length}
    )
    response.iter_content.return_value = [b"sa", b"fe"]
    monkeypatch.setattr(module.requests, "get", lambda *_args, **_kwargs: response)
    monkeypatch.setattr(module.tempfile, "mkdtemp", lambda **_kwargs: str(download_dir))

    installer_path = downloader.download_installer()

    assert Path(installer_path).read_bytes() == b"safe"


@pytest.mark.parametrize(
    ("module", "downloader_class"),
    DOWNLOADER_IMPLEMENTATIONS,
)
def test_bootstrapper_rejects_oversized_chunk_before_write(
    monkeypatch, tmp_path, module, downloader_class
):
    download_dir = tmp_path / "download"
    download_dir.mkdir()
    part_path = download_dir / f"{OFFLINE_NAME}.part"
    part_path.write_bytes(b"")
    downloader = downloader_class()
    _configure_download(downloader, b"safe")

    response = MagicMock()
    response.headers = {}
    response.iter_content.return_value = [b"safe!"]
    monkeypatch.setattr(module.requests, "get", lambda *_args, **_kwargs: response)
    monkeypatch.setattr(module.tempfile, "mkdtemp", lambda **_kwargs: str(download_dir))
    file_handle = MagicMock()
    file_handle.__enter__.return_value = file_handle
    open_mock = MagicMock(return_value=file_handle)
    monkeypatch.setattr(module, "open", open_mock, raising=False)
    progress = MagicMock()

    with pytest.raises(module.DownloadError, match="exceeds expected size"):
        downloader.download_installer(progress_callback=progress)

    progress.assert_not_called()
    file_handle.write.assert_not_called()
    assert not (download_dir / OFFLINE_NAME).exists()
    assert not part_path.exists()
    assert not download_dir.exists()


def test_bootstrapper_import_does_not_require_update_downloader():
    command = "\n".join(
        [
            "import sys",
            "import bootstrapper.downloader",
            "import bootstrapper.bootstrapper",
            "assert 'src.core.update_downloader' not in sys.modules",
        ]
    )

    result = subprocess.run(
        [sys.executable, "-c", command],
        cwd=Path(__file__).parents[1],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_bootstrapper_unowned_integrity_failure_does_not_delete_path(
    monkeypatch, tmp_path
):
    installer = tmp_path / OFFLINE_NAME
    installer.write_bytes(b"tampered")
    app = bundled_bootstrapper.BootstrapperApp.__new__(
        bundled_bootstrapper.BootstrapperApp
    )
    app.downloaded_file = str(installer)
    app.downloader = bundled_bootstrapper.InstallerDownloader()
    app.downloader.expected_sha256 = hashlib.sha256(b"expected").hexdigest()
    app.downloader.file_size = len(b"expected")
    errors = []
    app._show_error = errors.append
    popen = MagicMock()
    monkeypatch.setattr(bundled_bootstrapper.subprocess, "Popen", popen)

    app._launch_installer()

    assert installer.read_bytes() == b"tampered"
    assert errors
    popen.assert_not_called()


@pytest.mark.parametrize(
    ("module", "downloader_class"),
    DOWNLOADER_IMPLEMENTATIONS,
)
def test_bootstrapper_downloaders_expose_verified_file_lease(
    module, downloader_class, tmp_path
):
    installer = tmp_path / OFFLINE_NAME
    installer.write_bytes(b"safe")
    downloader = downloader_class()
    downloader.expected_sha256 = hashlib.sha256(b"safe").hexdigest()
    downloader.file_size = 4
    dispatched = []

    with downloader.open_verified_installer(str(installer)) as verified:
        source = verified._source
        verified.dispatch(dispatched.append)

    assert isinstance(verified, update_integrity.VerifiedFileLease)
    assert dispatched == [str(installer)]
    assert source.closed


def test_bootstrapper_verified_launch_closes_replacement_race(
    monkeypatch, tmp_path
):
    _force_bundled_bootstrapper_windows(monkeypatch)
    download_dir = tmp_path / "download"
    download_dir.mkdir()
    downloader = bundled_bootstrapper.InstallerDownloader()
    _configure_download(downloader, b"safe")
    response = MagicMock()
    response.headers = {"content-length": "4"}
    response.iter_content.return_value = [b"safe"]
    monkeypatch.setattr(
        bundled_bootstrapper.requests,
        "get",
        lambda *_args, **_kwargs: response,
    )
    monkeypatch.setattr(
        bundled_bootstrapper.tempfile,
        "mkdtemp",
        lambda **_kwargs: str(download_dir),
    )
    installer_path = downloader.download_installer()
    owner = downloader._find_owned_parent(installer_path)
    assert owner is not None
    owner.close()

    replacement_blocked = []
    leases = []
    open_verified = downloader.open_verified_installer

    def capture_lease(path):
        lease = open_verified(path)
        leases.append(lease)
        return lease

    monkeypatch.setattr(downloader, "open_verified_installer", capture_lease)
    app = bundled_bootstrapper.BootstrapperApp.__new__(
        bundled_bootstrapper.BootstrapperApp
    )
    app.downloaded_file = installer_path
    app.downloader = downloader
    app.root = MagicMock()
    errors = []
    app._show_error = errors.append
    popen_calls = []

    def popen(args, **kwargs):
        popen_calls.append((args, kwargs))
        assert leases[0].closed is False
        replacement = download_dir / "replacement.exe"
        replacement.write_bytes(b"safe")
        try:
            os.replace(replacement, installer_path)
        except PermissionError:
            replacement_blocked.append(True)
        return MagicMock()

    monkeypatch.setattr(bundled_bootstrapper.subprocess, "DETACHED_PROCESS", 0)
    monkeypatch.setattr(bundled_bootstrapper.subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    monkeypatch.setattr(bundled_bootstrapper.subprocess, "Popen", popen)

    app._launch_installer()

    assert len(popen_calls) == 1
    assert len(leases) == 1 and leases[0].closed is True
    if os.name == "nt":
        assert replacement_blocked == [True]
        assert errors == []
    else:
        assert replacement_blocked == []
        assert errors and "identity" in errors[0]

    post_context_replacement = download_dir / "post-context.exe"
    post_context_replacement.write_bytes(b"after")
    os.replace(post_context_replacement, installer_path)
    assert Path(installer_path).read_bytes() == b"after"
