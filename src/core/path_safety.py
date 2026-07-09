"""Path safety helpers for user-controlled output folders."""
from datetime import datetime
from pathlib import Path


def _safe_component(value: str) -> str:
    safe = str(value).replace(':', '_').replace('/', '_').replace('\\', '_')
    safe = safe.replace('*', '_').replace('?', '_').replace('"', '_')
    safe = safe.replace('<', '_').replace('>', '_').replace('|', '_')
    safe = safe.strip().strip(".")
    return safe if safe not in {".", ".."} else ""


def safe_output_dir(base_dir: str, folder_name: str) -> str:
    """Return a child output path under base_dir from a user-controlled name."""
    fallback = f"export_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    safe_folder_name = _safe_component(folder_name) or fallback
    base_path = Path(base_dir).expanduser().resolve()
    output_path = (base_path / safe_folder_name).resolve()
    try:
        if not output_path.is_relative_to(base_path):
            output_path = (base_path / fallback).resolve()
    except ValueError:
        output_path = (base_path / fallback).resolve()
    return str(output_path)
