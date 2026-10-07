"""Windows VERSIONINFO for PyInstaller specs.

SignPath requires ProductName/ProductVersion on every signed PE file, so the
version is taken from src/version.py (single source of truth) at build time.
"""

from src.version import __app_name__, __version__


def version_tuple(version: str) -> tuple[int, int, int, int]:
    parts = [int(p) for p in version.split("-")[0].split(".")]
    return tuple((parts + [0, 0, 0, 0])[:4])


def build_version_info(file_description: str, original_filename: str, version: str = __version__):
    from PyInstaller.utils.win32.versioninfo import (
        FixedFileInfo,
        StringFileInfo,
        StringStruct,
        StringTable,
        VarFileInfo,
        VarStruct,
        VSVersionInfo,
    )

    vers = version_tuple(version)
    strings = {
        "CompanyName": "sanghyun-io",
        "FileDescription": file_description,
        "FileVersion": version,
        "InternalName": original_filename.rsplit(".", 1)[0],
        "LegalCopyright": "MIT License",
        "OriginalFilename": original_filename,
        "ProductName": __app_name__,
        "ProductVersion": version,
    }
    return VSVersionInfo(
        ffi=FixedFileInfo(filevers=vers, prodvers=vers),
        kids=[
            StringFileInfo([StringTable("040904B0", [StringStruct(k, v) for k, v in strings.items()])]),
            VarFileInfo([VarStruct("Translation", [1033, 1200])]),
        ],
    )
