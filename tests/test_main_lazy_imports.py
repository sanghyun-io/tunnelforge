"""main.py의 _lazy_class() 문자열 import가 실제로 해석되고 PyInstaller 번들에도 포함되는지 검증."""
import importlib
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LAZY_TARGETS = re.findall(r'_lazy_class\("(src\.[\w.]+)", "(\w+)"\)', (ROOT / "main.py").read_text(encoding="utf-8"))


def test_lazy_targets_resolve():
    assert LAZY_TARGETS
    for module_path, name in LAZY_TARGETS:
        assert hasattr(importlib.import_module(module_path), name), f"{module_path}.{name}"


def test_lazy_targets_are_bundled():
    spec = (ROOT / "tunnel-manager.spec").read_text(encoding="utf-8")
    collected = re.findall(r"collect_submodules\('([\w.]+)'\)", spec)
    for module_path, _ in LAZY_TARGETS:
        assert f"'{module_path}'" in spec or any(module_path.startswith(p + ".") for p in collected), module_path
