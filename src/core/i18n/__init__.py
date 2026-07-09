"""TunnelForge runtime internationalization package.

Three layers behind one stable import surface:

* :mod:`src.core.i18n.keys` -- structured ``tr()`` catalog, active-language
  state, and language selection (CLI args, installer hint, saved setting, OS
  locale).
* :mod:`src.core.i18n.legacy_translate` -- best-effort auto-translation of
  hardcoded Korean UI strings that predate the structured catalog.
* :mod:`src.core.i18n.qt_hooks` -- one-time PyQt widget-text patching that
  routes legacy strings through the auto-translation layer at render time.

Existing call sites import from ``src.core.i18n``; the names below are
re-exported so the package split stays invisible to consumers.
"""
from src.core.i18n.keys import (
    DEFAULT_LANGUAGE,
    INSTALLER_LANGUAGE_HINT_FILE,
    SUPPORTED_LANGUAGES,
    configure_language,
    consume_installer_language_hint,
    current_language,
    detect_system_language,
    installer_language_hint_path,
    language_from_args,
    language_label,
    normalize_language,
    read_installer_language_hint,
    set_language,
    tr,
)
from src.core.i18n.legacy_translate import _translate_sequence, translate_text
from src.core.i18n.qt_hooks import install_qt_i18n

__all__ = [
    "DEFAULT_LANGUAGE",
    "INSTALLER_LANGUAGE_HINT_FILE",
    "SUPPORTED_LANGUAGES",
    "configure_language",
    "consume_installer_language_hint",
    "current_language",
    "detect_system_language",
    "install_qt_i18n",
    "installer_language_hint_path",
    "language_from_args",
    "language_label",
    "normalize_language",
    "read_installer_language_hint",
    "set_language",
    "tr",
    "translate_text",
]
