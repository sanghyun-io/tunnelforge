"""Regression guard: tests must never open real windows on a developer's screen."""
import sys

from PyQt6.QtWidgets import QApplication


def test_pytest_session_uses_the_offscreen_qt_platform():
    app = QApplication.instance() or QApplication(sys.argv)
    assert app.platformName() == "offscreen"
