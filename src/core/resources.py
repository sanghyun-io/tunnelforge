"""Resource path helpers shared by source and packaged runs."""
import platform
import sys
from pathlib import Path
from typing import Optional


def project_root() -> Path:
    if hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)  # type: ignore[attr-defined]
    return Path(__file__).resolve().parents[2]


def _runtime_resource_roots() -> tuple[Path, ...]:
    roots = [project_root()]

    if getattr(sys, "frozen", False):
        executable_dir = Path(sys.executable).parent
        contents_dir = executable_dir.parent
        if executable_dir.name == "MacOS" and contents_dir.name == "Contents":
            roots.extend([
                contents_dir / "Resources",
                contents_dir / "Frameworks",
            ])

    return tuple(dict.fromkeys(roots))


def resource_path(relative_path: str, base_dir: Optional[Path] = None) -> Path:
    if base_dir is not None:
        return Path(base_dir) / relative_path

    for root in _runtime_resource_roots():
        candidate = root / relative_path
        if candidate.exists():
            return candidate
    return _runtime_resource_roots()[0] / relative_path


def app_icon_path(
    base_dir: Optional[Path] = None,
    platform_name: Optional[str] = None,
) -> Path:
    system = platform_name or platform.system()
    candidates = (
        ("assets/icon.ico", "assets/icon.png")
        if system == "Windows"
        else ("assets/icon.icns", "assets/icon.png", "assets/icon_512.png", "assets/icon.ico")
    )

    for candidate in candidates:
        path = resource_path(candidate, base_dir)
        if path.exists():
            return path
    return resource_path(candidates[0], base_dir)
