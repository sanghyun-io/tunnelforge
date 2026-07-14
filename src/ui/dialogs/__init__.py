from .tunnel_config import TunnelConfigDialog
from .settings import SettingsDialog, CloseConfirmDialog
from .db_dialogs import RustDumpWizard
from .migration_dialogs import MigrationAnalyzerDialog, MigrationWizard
from .oneclick_migration_dialog import OneClickMigrationDialog
from .test_dialogs import SQLExecutionDialog, TestProgressDialog
from .sql_editor_dialog import SQLEditorDialog
from .error_reporting_consent_dialog import ErrorReportingConsentDialog

__all__ = [
    'TunnelConfigDialog', 'SettingsDialog', 'CloseConfirmDialog', 'RustDumpWizard',
    'MigrationAnalyzerDialog', 'MigrationWizard', 'OneClickMigrationDialog',
    'SQLExecutionDialog', 'TestProgressDialog', 'SQLEditorDialog',
    'ErrorReportingConsentDialog'
]
