"""Wizard launch helpers for the main window."""

from typing import Optional

from src.ui.dialogs.cross_engine_migration_dialog import CrossEngineMigrationWizard
from src.ui.dialogs.db_dialogs import RustDumpWizard
from src.ui.dialogs.migration_dialogs import MigrationWizard


class WizardLauncher:
    """Creates and launches main-window wizard dialogs."""

    def __init__(self, window):
        self._window = window

    def _launch_rust_dump_wizard(self, action: str, tunnel: Optional[dict] = None):
        kwargs = {
            "parent": self._window,
            "tunnel_engine": self._window.engine,
            "config_manager": self._window.config_mgr,
        }
        if tunnel is not None:
            kwargs["preselected_tunnel"] = tunnel

        wizard = RustDumpWizard(**kwargs)
        getattr(wizard, action)()

    def open_rust_dump_export(self):
        self._launch_rust_dump_wizard("start_export")

    def open_rust_dump_import(self):
        self._launch_rust_dump_wizard("start_import")

    def open_migration_analyzer(self):
        MigrationWizard.start(
            parent=self._window,
            tunnel_engine=self._window.engine,
            config_manager=self._window.config_mgr,
        )

    def open_cross_engine_migration(self):
        CrossEngineMigrationWizard.start(
            parent=self._window,
            tunnel_engine=self._window.engine,
            config_manager=self._window.config_mgr,
        )
