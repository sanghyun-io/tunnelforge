"""PyQt widget-text patching for legacy hardcoded UI strings.

Installs one-time monkey-patches over common PyQt6 widget constructors and
text setters so Korean literals still embedded in the UI are auto-translated
through :func:`src.core.i18n.legacy_translate.translate_text` at render time.
Identity-like values (for example combo-box schema/database entries) are
deliberately left untranslated.
"""
from src.core.i18n.legacy_translate import _translate_sequence, translate_text

_qt_i18n_installed = False


def _wrap_callable(container, name, transform):
    """Wrap ``container.name`` so string arguments are translated first.

    ``transform`` receives ``(args, kwargs)`` -- ``args`` is a mutable list and
    ``kwargs`` the original dict -- and returns the ``(args, kwargs)`` forwarded
    to the original callable. The wrapper is idempotent via the
    ``_tf_i18n_wrapped`` guard, so repeated installs are safe.
    """
    original = getattr(container, name, None)
    if original is None or getattr(original, "_tf_i18n_wrapped", False):
        return

    def wrapped(*args, **kwargs):
        new_args, new_kwargs = transform(list(args), kwargs)
        return original(*new_args, **new_kwargs)

    wrapped._tf_i18n_wrapped = True
    setattr(container, name, wrapped)


def _translate_header_labels(args, kwargs):
    # Instance method (setHeaderLabels / setHorizontal/VerticalHeaderLabels):
    # args[0] is self, args[1] the label sequence.
    args[1] = _translate_sequence(list(args[1]))
    return args, kwargs


def _translate_tooltip_text(args, kwargs):
    # Static QToolTip.showText(pos, text, ...): translate the text arg only.
    if len(args) > 1 and isinstance(args[1], str):
        args[1] = translate_text(args[1])
    return args, kwargs


def _translate_menu_label(args, kwargs):
    # Instance QMenu.addAction / addMenu: args[0] is self; the label is the
    # first str after self, or the second when the first arg is an icon.
    if len(args) > 1 and isinstance(args[1], str):
        args[1] = translate_text(args[1])
    elif len(args) > 2 and isinstance(args[2], str):
        args[2] = translate_text(args[2])
    return args, kwargs


def _translate_messagebox_static(args, kwargs):
    # Static QMessageBox.information/warning/critical/question: title/text.
    for index in (1, 2):
        if len(args) > index and isinstance(args[index], str):
            args[index] = translate_text(args[index])
    if isinstance(kwargs.get("title"), str):
        kwargs["title"] = translate_text(kwargs["title"])
    if isinstance(kwargs.get("text"), str):
        kwargs["text"] = translate_text(kwargs["text"])
    return args, kwargs


def _translate_filedialog_static(args, kwargs):
    # Static QFileDialog.get*: caption (index 1) and filter (index 3).
    for index in (1, 3):
        if len(args) > index and isinstance(args[index], str):
            args[index] = translate_text(args[index])
    if isinstance(kwargs.get("caption"), str):
        kwargs["caption"] = translate_text(kwargs["caption"])
    if isinstance(kwargs.get("filter"), str):
        kwargs["filter"] = translate_text(kwargs["filter"])
    return args, kwargs


def install_qt_i18n() -> bool:
    """Install broad PyQt text translation hooks for legacy hardcoded UI strings."""
    global _qt_i18n_installed
    if _qt_i18n_installed:
        return False

    try:
        from PyQt6.QtGui import QAction
        from PyQt6.QtWidgets import (
            QAbstractButton,
            QCheckBox,
            QComboBox,
            QDialog,
            QFileDialog,
            QFormLayout,
            QGroupBox,
            QLabel,
            QLineEdit,
            QMenu,
            QMessageBox,
            QPlainTextEdit,
            QProgressBar,
            QPushButton,
            QRadioButton,
            QStatusBar,
            QSystemTrayIcon,
            QTabWidget,
            QTableWidget,
            QTextEdit,
            QToolTip,
            QTreeWidget,
            QWidget,
            QWizard,
            QWizardPage,
        )
    except Exception:
        return False

    def translate_qt_arg(value):
        if isinstance(value, str):
            return translate_text(value)
        if isinstance(value, list):
            return [translate_qt_arg(item) for item in value]
        if isinstance(value, tuple):
            return tuple(translate_qt_arg(item) for item in value)
        return value

    def patch_init(cls, text_index=0):
        original = cls.__init__
        if getattr(original, "_tf_i18n_wrapped", False):
            return

        def wrapped(self, *args, **kwargs):
            args = list(args)
            if text_index is None:
                args = [translate_qt_arg(arg) for arg in args]
            elif len(args) > text_index:
                args[text_index] = translate_qt_arg(args[text_index])
            original(self, *args, **kwargs)

        wrapped._tf_i18n_wrapped = True
        cls.__init__ = wrapped

    def patch_method(cls, name, text_indexes):
        original = getattr(cls, name, None)
        if original is None or getattr(original, "_tf_i18n_wrapped", False):
            return

        def wrapped(self, *args, **kwargs):
            args = list(args)
            for index in text_indexes:
                if len(args) > index:
                    args[index] = translate_qt_arg(args[index])
            return original(self, *args, **kwargs)

        wrapped._tf_i18n_wrapped = True
        setattr(cls, name, wrapped)

    def patch_all_string_args_method(cls, name):
        original = getattr(cls, name, None)
        if original is None or getattr(original, "_tf_i18n_wrapped", False):
            return

        def wrapped(self, *args, **kwargs):
            args = [translate_qt_arg(arg) for arg in args]
            return original(self, *args, **kwargs)

        wrapped._tf_i18n_wrapped = True
        setattr(cls, name, wrapped)

    for cls in (QLabel, QAbstractButton, QPushButton, QCheckBox, QRadioButton, QGroupBox, QAction, QMenu):
        patch_init(cls, None)
        patch_method(cls, "setText", [0])

    patch_init(QMessageBox, None)
    patch_method(QWidget, "setWindowTitle", [0])
    patch_method(QWidget, "setToolTip", [0])
    patch_method(QWidget, "setStatusTip", [0])
    patch_method(QWidget, "setWhatsThis", [0])
    patch_method(QDialog, "setWindowTitle", [0])
    patch_method(QGroupBox, "setTitle", [0])
    patch_method(QMenu, "setTitle", [0])
    patch_method(QAction, "setToolTip", [0])
    patch_method(QAction, "setStatusTip", [0])
    patch_method(QStatusBar, "showMessage", [0])
    patch_method(QSystemTrayIcon, "showMessage", [0, 1])
    patch_method(QProgressBar, "setFormat", [0])
    patch_method(QWizard, "setButtonText", [1])
    patch_method(QWizardPage, "setTitle", [0])
    patch_method(QWizardPage, "setSubTitle", [0])

    for cls in (QLineEdit, QTextEdit, QPlainTextEdit):
        patch_method(cls, "setPlaceholderText", [0])
    patch_method(QTextEdit, "append", [0])
    patch_method(QTextEdit, "setHtml", [0])
    patch_method(QPlainTextEdit, "appendPlainText", [0])

    patch_method(QComboBox, "setPlaceholderText", [0])
    # Combo inserted items can be schema/database identifiers; preserve them exactly.
    patch_method(QComboBox, "setItemText", [1])

    patch_all_string_args_method(QTabWidget, "addTab")
    patch_method(QTabWidget, "setTabText", [1])
    patch_all_string_args_method(QFormLayout, "addRow")

    _wrap_callable(QTreeWidget, "setHeaderLabels", _translate_header_labels)
    _wrap_callable(QTableWidget, "setHorizontalHeaderLabels", _translate_header_labels)
    _wrap_callable(QTableWidget, "setVerticalHeaderLabels", _translate_header_labels)

    _wrap_callable(QToolTip, "showText", _translate_tooltip_text)

    _wrap_callable(QMenu, "addAction", _translate_menu_label)
    _wrap_callable(QMenu, "addMenu", _translate_menu_label)

    for name in ("information", "warning", "critical", "question"):
        _wrap_callable(QMessageBox, name, _translate_messagebox_static)

    patch_method(QMessageBox, "setText", [0])
    patch_method(QMessageBox, "setInformativeText", [0])
    patch_method(QMessageBox, "setDetailedText", [0])

    for name in ("getOpenFileName", "getSaveFileName", "getExistingDirectory"):
        _wrap_callable(QFileDialog, name, _translate_filedialog_static)

    _qt_i18n_installed = True
    return True
