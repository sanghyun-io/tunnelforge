"""Shared release-package integrity and ownership primitives."""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import stat
from typing import Callable, Iterable, Optional, TypeVar


SHA256_DIGEST_RE = re.compile(r"^sha256:([0-9a-fA-F]{64})$")

# Release installers are expected to remain well below this 2 GiB safety cap.
MAX_INSTALLER_SIZE = 2 * 1024 * 1024 * 1024
_REPARSE_POINT_ATTRIBUTE = 0x00000400


class IntegrityError(ValueError):
    pass


def parse_sha256_digest(raw: object) -> str:
    match = SHA256_DIGEST_RE.fullmatch(str(raw or "").strip())
    if not match:
        raise IntegrityError("release asset SHA-256 digest is missing or invalid")
    return match.group(1).lower()


def parse_content_length(raw: object) -> Optional[int]:
    """Return a canonical Content-Length value, or None for invalid headers."""
    if not isinstance(raw, str) or not raw or not raw.isascii() or not raw.isdigit():
        return None
    return int(raw)


if os.name == "nt":
    import ctypes
    from ctypes import wintypes
    import msvcrt

    _GENERIC_READ = 0x80000000
    _DELETE = 0x00010000
    _FILE_LIST_DIRECTORY = 0x0001
    _FILE_READ_ATTRIBUTES = 0x0080
    _FILE_SHARE_READ = 0x00000001
    _FILE_SHARE_WRITE = 0x00000002
    _FILE_SHARE_DELETE = 0x00000004
    _OPEN_EXISTING = 3
    _FILE_ATTRIBUTE_DIRECTORY = 0x00000010
    _FILE_ATTRIBUTE_REPARSE_POINT = _REPARSE_POINT_ATTRIBUTE
    _FILE_FLAG_SEQUENTIAL_SCAN = 0x08000000
    _FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
    _FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    _FILE_DISPOSITION_INFO_CLASS = 4
    _INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    class _BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("dwFileAttributes", wintypes.DWORD),
            ("ftCreationTime", wintypes.FILETIME),
            ("ftLastAccessTime", wintypes.FILETIME),
            ("ftLastWriteTime", wintypes.FILETIME),
            ("dwVolumeSerialNumber", wintypes.DWORD),
            ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD),
            ("nNumberOfLinks", wintypes.DWORD),
            ("nFileIndexHigh", wintypes.DWORD),
            ("nFileIndexLow", wintypes.DWORD),
        ]

    class _FILE_DISPOSITION_INFO(ctypes.Structure):
        _fields_ = [("DeleteFile", wintypes.BOOL)]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _CreateFileW = _kernel32.CreateFileW
    _CreateFileW.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    _CreateFileW.restype = wintypes.HANDLE
    _GetFileInformationByHandle = _kernel32.GetFileInformationByHandle
    _GetFileInformationByHandle.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(_BY_HANDLE_FILE_INFORMATION),
    )
    _GetFileInformationByHandle.restype = wintypes.BOOL
    _SetFileInformationByHandle = _kernel32.SetFileInformationByHandle
    _SetFileInformationByHandle.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    )
    _SetFileInformationByHandle.restype = wintypes.BOOL
    _CloseHandle = _kernel32.CloseHandle
    _CloseHandle.argtypes = (wintypes.HANDLE,)
    _CloseHandle.restype = wintypes.BOOL


def _absolute_path(path: str | os.PathLike[str]) -> str:
    return os.path.normpath(os.path.abspath(os.fspath(path)))


def _same_path(left: str, right: str) -> bool:
    return os.path.normcase(left) == os.path.normcase(right)


def _is_safe_directory(entry: os.stat_result) -> bool:
    return (
        stat.S_ISDIR(entry.st_mode)
        and not stat.S_ISLNK(entry.st_mode)
        and not (
            getattr(entry, "st_file_attributes", 0) & _REPARSE_POINT_ATTRIBUTE
        )
    )


def _windows_open_handle(
    path: str,
    *,
    desired_access: int,
    share_mode: int,
    directory: bool,
) -> int:
    flags = _FILE_FLAG_OPEN_REPARSE_POINT
    flags |= _FILE_FLAG_BACKUP_SEMANTICS if directory else _FILE_FLAG_SEQUENTIAL_SCAN
    handle = _CreateFileW(
        path,
        desired_access,
        share_mode,
        None,
        _OPEN_EXISTING,
        flags,
        None,
    )
    if handle == _INVALID_HANDLE_VALUE:
        raise ctypes.WinError(ctypes.get_last_error())
    return handle


def _windows_handle_information(handle: int) -> tuple[tuple[int, int], int]:
    information = _BY_HANDLE_FILE_INFORMATION()
    if not _GetFileInformationByHandle(handle, ctypes.byref(information)):
        raise ctypes.WinError(ctypes.get_last_error())
    identity = (
        information.dwVolumeSerialNumber,
        (information.nFileIndexHigh << 32) | information.nFileIndexLow,
    )
    return identity, information.dwFileAttributes


def _windows_close_handle(handle: int) -> None:
    if not _CloseHandle(handle):
        raise ctypes.WinError(ctypes.get_last_error())


def _windows_close_handle_safely(handle: int) -> bool:
    try:
        _windows_close_handle(handle)
        return True
    except OSError:
        return False


def _close_descriptor_safely(descriptor: int) -> bool:
    try:
        os.close(descriptor)
        return True
    except OSError:
        return False


def _windows_path_identity(path: str, *, directory: bool) -> tuple[int, int]:
    share_mode = _FILE_SHARE_READ
    if directory:
        # The retained directory token requests DELETE access, so this observer
        # must share DELETE even though the retained token still denies it.
        share_mode |= _FILE_SHARE_WRITE | _FILE_SHARE_DELETE
    handle = _windows_open_handle(
        path,
        desired_access=_FILE_READ_ATTRIBUTES,
        share_mode=share_mode,
        directory=directory,
    )
    result = None
    primary_error = None
    try:
        identity, attributes = _windows_handle_information(handle)
        if attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
            raise IntegrityError("release path is a reparse point")
        is_directory = bool(attributes & _FILE_ATTRIBUTE_DIRECTORY)
        if is_directory != directory:
            raise IntegrityError("release path type changed")
        result = identity
    except Exception as exc:
        primary_error = exc
    finally:
        close_ok = _windows_close_handle_safely(handle)
    if primary_error is not None:
        raise primary_error
    if not close_ok:
        raise OSError("could not close release path handle")
    return result


def _windows_mark_for_delete(handle: int) -> bool:
    disposition = _FILE_DISPOSITION_INFO(True)
    return bool(
        _SetFileInformationByHandle(
            handle,
            _FILE_DISPOSITION_INFO_CLASS,
            ctypes.byref(disposition),
            ctypes.sizeof(disposition),
        )
    )


def _windows_file_identity(path: str) -> Optional[tuple[int, int]]:
    handle = None
    identity = None
    close_ok = False
    try:
        handle = _windows_open_handle(
            path,
            desired_access=_FILE_READ_ATTRIBUTES,
            share_mode=_FILE_SHARE_READ | _FILE_SHARE_WRITE,
            directory=False,
        )
        candidate_identity, attributes = _windows_handle_information(handle)
        if not (
            attributes & _FILE_ATTRIBUTE_REPARSE_POINT
            or attributes & _FILE_ATTRIBUTE_DIRECTORY
        ):
            identity = candidate_identity
    except OSError:
        pass
    finally:
        if handle is not None:
            close_ok = _windows_close_handle_safely(handle)
    return identity if close_ok else None


def _windows_delete_owned_child(
    path: str, expected_identity: tuple[int, int]
) -> bool:
    """Delete one recorded regular file by its own no-follow handle."""
    handle = None
    delete_requested = False
    close_ok = False
    try:
        handle = _windows_open_handle(
            path,
            desired_access=_DELETE | _FILE_READ_ATTRIBUTES,
            share_mode=_FILE_SHARE_READ | _FILE_SHARE_WRITE,
            directory=False,
        )
        identity, attributes = _windows_handle_information(handle)
        if (
            identity != expected_identity
            or attributes & _FILE_ATTRIBUTE_REPARSE_POINT
            or attributes & _FILE_ATTRIBUTE_DIRECTORY
        ):
            return False
        delete_requested = _windows_mark_for_delete(handle)
    except OSError:
        pass
    finally:
        if handle is not None:
            close_ok = _windows_close_handle_safely(handle)
    return delete_requested and close_ok


_DispatchResult = TypeVar("_DispatchResult")


class VerifiedFileLease:
    """A verified no-follow file lease retained through launch dispatch."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        expected_sha256: str,
        expected_size: int,
    ):
        self.path = _absolute_path(path)
        self.expected_sha256 = expected_sha256
        self.expected_size = expected_size
        self._source = None
        self._identity = None

    def __enter__(self) -> "VerifiedFileLease":
        if self.expected_size <= 0:
            raise IntegrityError("release asset size must be positive")
        try:
            self._source, self._identity = self._open_source()
            source_stat = os.fstat(self._source.fileno())
            if source_stat.st_size != self.expected_size:
                raise IntegrityError(
                    "downloaded file size does not match release metadata"
                )

            hasher = hashlib.sha256()
            for chunk in iter(lambda: self._source.read(1024 * 1024), b""):
                hasher.update(chunk)
            if not hmac.compare_digest(hasher.hexdigest(), self.expected_sha256):
                raise IntegrityError(
                    "downloaded file SHA-256 does not match release metadata"
                )
            self._source.seek(0)
            return self
        except IntegrityError:
            self._close_after_failed_enter()
            raise
        except OSError as exc:
            self._close_after_failed_enter()
            raise IntegrityError(f"could not open release file securely: {exc}") from exc

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        if _exc_type is None:
            self.close()
        else:
            self._close_after_failed_enter()

    def _open_source(self):
        if os.name == "nt":
            handle = _windows_open_handle(
                self.path,
                desired_access=_GENERIC_READ,
                share_mode=_FILE_SHARE_READ,
                directory=False,
            )
            descriptor = None
            try:
                identity, attributes = _windows_handle_information(handle)
                if attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
                    raise IntegrityError("release file is a reparse point")
                if attributes & _FILE_ATTRIBUTE_DIRECTORY:
                    raise IntegrityError("release path is not a regular file")
                descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
                handle = None
                source = os.fdopen(descriptor, "rb")
                descriptor = None
                return source, identity
            finally:
                if descriptor is not None:
                    _close_descriptor_safely(descriptor)
                if handle is not None:
                    _windows_close_handle(handle)

        no_follow = getattr(os, "O_NOFOLLOW", 0)
        if not no_follow:
            raise IntegrityError("no-follow file opens are unavailable")
        flags = os.O_RDONLY | no_follow | getattr(os, "O_CLOEXEC", 0)
        descriptor = os.open(self.path, flags)
        try:
            source_stat = os.fstat(descriptor)
            if not stat.S_ISREG(source_stat.st_mode):
                raise IntegrityError("release path is not a regular file")
            identity = (source_stat.st_dev, source_stat.st_ino)
            source = os.fdopen(descriptor, "rb")
            descriptor = None
            return source, identity
        finally:
            if descriptor is not None:
                _close_descriptor_safely(descriptor)

    def _assert_dispatch_identity(self) -> None:
        if self._source is None or self._identity is None:
            raise IntegrityError("verified release file lease is not active")
        try:
            if os.name == "nt":
                current_identity = _windows_path_identity(self.path, directory=False)
            else:
                current = os.lstat(self.path)
                if not stat.S_ISREG(current.st_mode):
                    raise IntegrityError("release path is not a regular file")
                current_identity = (current.st_dev, current.st_ino)
        except IntegrityError:
            raise
        except OSError as exc:
            raise IntegrityError(
                f"release file path identity is unavailable: {exc}"
            ) from exc
        if current_identity != self._identity:
            raise IntegrityError("release file path identity changed before dispatch")

    def dispatch(
        self, callback: Callable[[str], _DispatchResult]
    ) -> _DispatchResult:
        self._assert_dispatch_identity()
        try:
            return callback(self.path)
        finally:
            self._assert_dispatch_identity()

    @property
    def closed(self) -> bool:
        return self._source is None or self._source.closed

    def close(self) -> None:
        if self._source is not None:
            source = self._source
            self._source = None
            source.close()

    def _close_after_failed_enter(self) -> None:
        try:
            self.close()
        except Exception:
            pass


# Compatibility for tests and callers introduced during the staged hardening.
VerifiedLaunchFile = VerifiedFileLease


class OwnedTempDirectory:
    """Identity token for downloader cleanup with platform-specific deletion.

    Windows records each retained final child identity and deletes only through a
    matching no-follow child handle. POSIX has no standard identity-conditional
    unlink primitive, so this helper deliberately does not delete files or the
    temporary root there and returns ``False`` instead.
    """

    def __init__(self, path: str | os.PathLike[str]):
        self.path = _absolute_path(path)
        self._handle = None
        self._descriptor = None
        self._owned_child_names: set[str] = set()
        self._child_identities: dict[str, tuple[int, int]] = {}

        try:
            parent_lstat = os.lstat(self.path)
            if not _is_safe_directory(parent_lstat):
                raise IntegrityError("temporary parent is not a safe directory")
            self._path_identity = (parent_lstat.st_dev, parent_lstat.st_ino)

            if os.name == "nt":
                capture_handle = _windows_open_handle(
                    self.path,
                    desired_access=_FILE_LIST_DIRECTORY | _FILE_READ_ATTRIBUTES,
                    share_mode=(
                        _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE
                    ),
                    directory=True,
                )
                primary_error = None
                try:
                    self.identity, attributes = _windows_handle_information(
                        capture_handle
                    )
                    if attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
                        raise IntegrityError("temporary parent is a reparse point")
                    if not attributes & _FILE_ATTRIBUTE_DIRECTORY:
                        raise IntegrityError("temporary parent is not a directory")
                except Exception as exc:
                    primary_error = exc
                finally:
                    close_ok = _windows_close_handle_safely(capture_handle)
                if primary_error is not None:
                    raise primary_error
                if not close_ok:
                    raise OSError("could not close temporary parent handle")
            else:
                no_follow = getattr(os, "O_NOFOLLOW", 0)
                if not no_follow:
                    raise IntegrityError("no-follow directory opens are unavailable")
                flags = (
                    os.O_RDONLY
                    | no_follow
                    | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_CLOEXEC", 0)
                )
                self._descriptor = os.open(self.path, flags)
                parent_stat = os.fstat(self._descriptor)
                if not _is_safe_directory(parent_stat):
                    raise IntegrityError("temporary parent is not a directory")
                self.identity = (parent_stat.st_dev, parent_stat.st_ino)
                if self.identity != self._path_identity:
                    raise IntegrityError("temporary parent identity changed")
        except Exception:
            self.close()
            raise

    def _is_direct_child(self, path: str | os.PathLike[str]) -> bool:
        candidate = _absolute_path(path)
        parent, name = os.path.split(candidate)
        return (
            _same_path(parent, self.path)
            and bool(name)
            and name not in {".", ".."}
        )

    def claim_files(self, paths: Iterable[str | os.PathLike[str]]) -> bool:
        """Register the exact direct child names created by this downloader."""
        candidates = [_absolute_path(path) for path in paths]
        if not all(self._is_direct_child(candidate) for candidate in candidates):
            return False
        self._owned_child_names.update(os.path.basename(path) for path in candidates)
        return True

    def owns_direct_child(self, path: str | os.PathLike[str]) -> bool:
        candidate = _absolute_path(path)
        return (
            self._is_direct_child(candidate)
            and os.path.basename(candidate) in self._owned_child_names
        )

    def identity_matches(self) -> bool:
        try:
            current_lstat = os.lstat(self.path)
            if (
                not _is_safe_directory(current_lstat)
                or (current_lstat.st_dev, current_lstat.st_ino)
                != self._path_identity
            ):
                return False
            if os.name == "nt":
                if self._handle is None and not self.retain():
                    return False
                return _windows_path_identity(
                    self.path, directory=True
                ) == self.identity
            current = os.stat(self.path, follow_symlinks=False)
            return _is_safe_directory(current) and (
                current.st_dev,
                current.st_ino,
            ) == self.identity
        except (IntegrityError, OSError):
            return False

    def retain(
        self,
        final_path: str | os.PathLike[str] | None = None,
        expected_child_identity: tuple[int, int] | None = None,
    ) -> bool:
        """Retain the parent and record a registered Windows child identity."""
        if os.name != "nt":
            if final_path is not None:
                candidate = _absolute_path(final_path)
                if not self.owns_direct_child(candidate):
                    return False
                try:
                    child_stat = os.stat(
                        os.path.basename(candidate),
                        dir_fd=self._descriptor,
                        follow_symlinks=False,
                    )
                except OSError:
                    return False
                child_identity = (child_stat.st_dev, child_stat.st_ino)
                if (
                    not stat.S_ISREG(child_stat.st_mode)
                    or expected_child_identity is not None
                    and child_identity != expected_child_identity
                ):
                    return False
                self._child_identities[os.path.basename(candidate)] = child_identity
            return self.identity_matches()
        handle = None
        try:
            if self._handle is None:
                handle = _windows_open_handle(
                    self.path,
                    desired_access=(
                        _DELETE | _FILE_LIST_DIRECTORY | _FILE_READ_ATTRIBUTES
                    ),
                    share_mode=_FILE_SHARE_READ | _FILE_SHARE_WRITE,
                    directory=True,
                )
                current_identity, attributes = _windows_handle_information(handle)
                if (
                    current_identity != self.identity
                    or attributes & _FILE_ATTRIBUTE_REPARSE_POINT
                    or not attributes & _FILE_ATTRIBUTE_DIRECTORY
                ):
                    return False
                self._handle = handle
                handle = None
            if final_path is not None:
                candidate = _absolute_path(final_path)
                if not self.owns_direct_child(candidate):
                    return False
                child_identity = _windows_file_identity(candidate)
                if (
                    child_identity is None
                    or expected_child_identity is not None
                    and child_identity != expected_child_identity
                ):
                    return False
                self._child_identities[os.path.basename(candidate)] = child_identity
            return True
        except OSError:
            return False
        finally:
            if handle is not None:
                _windows_close_handle_safely(handle)

    def release_parent_handle(self) -> bool:
        """Release the Windows root lease only while renaming a verified part."""
        if os.name != "nt" or self._handle is None:
            return True
        handle = self._handle
        self._handle = None
        return _windows_close_handle_safely(handle)

    def child_identity(self, path: str | os.PathLike[str]) -> Optional[tuple[int, int]]:
        """Return a registered child identity without accepting unowned names."""
        candidate = _absolute_path(path)
        if not self.owns_direct_child(candidate):
            return None
        return self._child_identities.get(os.path.basename(candidate))

    def forget_child_identity(self, path: str | os.PathLike[str]) -> None:
        """Drop an identity only after a verified rename consumed that name."""
        candidate = _absolute_path(path)
        if self.owns_direct_child(candidate):
            self._child_identities.pop(os.path.basename(candidate), None)

    def discard_files(
        self, paths: Iterable[str | os.PathLike[str]]
    ) -> bool:
        if os.name != "nt":
            # `unlinkat` has no identity-conditional standard API. Do not risk
            # deleting a replacement path merely because a parent fd is held.
            return False
        candidates = []
        for path in paths:
            candidate = _absolute_path(path)
            if not self.owns_direct_child(candidate):
                return False
            candidates.append(candidate)

        if not self.identity_matches():
            return False

        for candidate in candidates:
            try:
                os.lstat(candidate)
            except FileNotFoundError:
                continue
            except OSError:
                return False
            expected_identity = self._child_identities.get(os.path.basename(candidate))
            if expected_identity is None:
                return False
            if not _windows_delete_owned_child(candidate, expected_identity):
                return False
        return True

    def remove_if_empty(self) -> bool:
        """Delete only a retained empty Windows root by its own handle.

        POSIX has no standard identity-conditional unlink/rmdir primitive, so
        this method deliberately returns ``False`` there without path deletion.
        """
        if os.name != "nt" or not self.identity_matches() or self._handle is None:
            return False
        handle = self._handle
        if not _windows_mark_for_delete(handle):
            return False
        self._handle = None
        return _windows_close_handle_safely(handle)

    def close(self) -> None:
        if self._handle is not None:
            handle = self._handle
            self._handle = None
            try:
                _windows_close_handle(handle)
            except OSError:
                pass
        if self._descriptor is not None:
            descriptor = self._descriptor
            self._descriptor = None
            try:
                os.close(descriptor)
            except OSError:
                pass

    def __del__(self):
        self.close()


def _publish_windows_no_clobber(part_path: str, final_path: str) -> None:
    # Windows rename preserves an existing destination instead of replacing it.
    os.rename(part_path, final_path)


def _publish_posix_no_clobber(
    part_path: str,
    final_path: str,
    expected_identity: tuple[int, int],
) -> None:
    # link(2) atomically requires a missing destination. Leave the part link in
    # place after success because standard POSIX has no conditional unlink.
    os.link(part_path, final_path, follow_symlinks=False)
    part_stat = os.stat(part_path, follow_symlinks=False)
    final_stat = os.stat(final_path, follow_symlinks=False)
    if (
        not stat.S_ISREG(part_stat.st_mode)
        or not stat.S_ISREG(final_stat.st_mode)
        or (part_stat.st_dev, part_stat.st_ino) != expected_identity
        or (final_stat.st_dev, final_stat.st_ino) != expected_identity
    ):
        raise IntegrityError("temporary download publish identity changed")


def publish_owned_temp_file(
    owner: OwnedTempDirectory,
    part_path: str | os.PathLike[str],
    final_path: str | os.PathLike[str],
) -> str:
    """Atomically publish a registered part without replacing a destination.

    POSIX intentionally retains the `.part` hard link after publication because
    standard APIs cannot conditionally unlink it by recorded identity.
    """
    part = _absolute_path(part_path)
    final = _absolute_path(final_path)
    expected_identity = owner.child_identity(part)
    if expected_identity is None or not owner.release_parent_handle():
        raise IntegrityError("temporary download publish identity changed")
    try:
        if os.name == "nt":
            _publish_windows_no_clobber(part, final)
        else:
            _publish_posix_no_clobber(part, final, expected_identity)
    except OSError as exc:
        raise IntegrityError(f"temporary download publish failed: {exc}") from exc

    if not owner.retain(final, expected_identity):
        raise IntegrityError("temporary download publish identity changed")
    if os.name == "nt":
        owner.forget_child_identity(part)
    return final


def verify_file_integrity(
    path: str | os.PathLike[str], expected_sha256: str, expected_size: int
) -> None:
    with VerifiedFileLease(path, expected_sha256, expected_size):
        pass
