"""Local-only consent prompt for anonymous error reporting."""

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QToolButton,
    QVBoxLayout,
)

from src.core.error_report_consent import PromptOutcome
from src.core.i18n import tr
from src.ui.styles import ButtonStyles, LabelStyles


class ErrorReportingConsentDialog(QDialog):
    """Present reporting consent without collecting or sending any data."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._outcome = PromptOutcome.LATER

        self.setWindowTitle(tr("error_reporting_consent.title"))
        self.setModal(True)
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setMinimumWidth(440)
        self.setMaximumWidth(620)
        self.setMaximumHeight(700)
        self.resize(520, 0)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(10)

        self.title_label = QLabel(tr("error_reporting_consent.title"), self)
        self.title_label.setStyleSheet(LabelStyles.HIGHLIGHT)
        self.title_label.setWordWrap(True)
        layout.addWidget(self.title_label)

        for key in (
            "error_reporting_consent.description",
            "error_reporting_consent.public_issue",
            "error_reporting_consent.settings_path",
        ):
            label = QLabel(tr(key), self)
            label.setWordWrap(True)
            if key == "error_reporting_consent.public_issue":
                label.setStyleSheet(LabelStyles.WARNING)
            elif key == "error_reporting_consent.settings_path":
                label.setStyleSheet(LabelStyles.CAPTION)
            layout.addWidget(label)

        self.collected_expander, self.collected_details_label = self._create_expander(
            "error_reporting_consent.collected",
            "error_reporting_consent.collected_details",
        )
        layout.addWidget(self.collected_expander)
        layout.addWidget(self.collected_details_label)

        self.excluded_expander, self.excluded_details_label = self._create_expander(
            "error_reporting_consent.excluded",
            "error_reporting_consent.excluded_details",
        )
        layout.addWidget(self.excluded_expander)
        layout.addWidget(self.excluded_details_label)

        self.suppression_checkbox = QCheckBox(
            tr("error_reporting_consent.suppress"), self
        )
        self.suppression_checkbox.setChecked(False)
        self.suppression_checkbox.setAccessibleName(
            tr("error_reporting_consent.suppress")
        )
        layout.addWidget(self.suppression_checkbox)

        buttons = QHBoxLayout()
        buttons.addStretch()
        self.later_button = QPushButton(tr("error_reporting_consent.later"), self)
        self.later_button.setStyleSheet(ButtonStyles.SECONDARY)
        self.later_button.setAccessibleName(tr("error_reporting_consent.later_accessible"))
        self.later_button.clicked.connect(self.reject)
        buttons.addWidget(self.later_button)

        self.enable_button = QPushButton(tr("error_reporting_consent.enable"), self)
        self.enable_button.setStyleSheet(ButtonStyles.PRIMARY)
        self.enable_button.setDefault(True)
        self.enable_button.setAccessibleName(tr("error_reporting_consent.enable"))
        self.enable_button.clicked.connect(self._enable)
        buttons.addWidget(self.enable_button)
        layout.addLayout(buttons)

    def _create_expander(self, title_key, details_key):
        button = QToolButton(self)
        button.setText(tr(title_key))
        button.setCheckable(True)
        button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        button.setArrowType(Qt.ArrowType.RightArrow)
        button.setAccessibleName(tr(title_key))

        details = QLabel(tr(details_key), self)
        details.setWordWrap(True)
        details.setVisible(False)
        details.setStyleSheet(LabelStyles.CAPTION)

        def toggle_details(checked):
            button.setArrowType(
                Qt.ArrowType.DownArrow if checked else Qt.ArrowType.RightArrow
            )
            details.setVisible(checked)

        button.toggled.connect(toggle_details)
        return button, details

    def _enable(self):
        self._outcome = PromptOutcome.ENABLE
        self.accept()

    def get_outcome(self):
        """Return the local choice and independent suppression preference."""
        return self._outcome, self.suppression_checkbox.isChecked()
