"""Mixin for dialogs with a collapsible top configuration section."""


class CollapsibleConfigDialog:
    """Require splitter, config_container, and btn_collapse attributes."""

    def toggle_config_section(self):
        """설정 섹션 접기/펼치기"""
        is_visible = self.config_container.isVisible()
        self.config_container.setVisible(not is_visible)
        self.btn_collapse.setText("🔼 설정 펼치기" if is_visible else "🔽 설정 접기")

    def collapse_config_section(self):
        """설정 섹션을 접음 (작업 시작 시)"""
        self.config_container.setVisible(False)
        self.btn_collapse.setText("🔼 설정 펼치기")
        self.btn_collapse.setVisible(True)

        total_height = self.splitter.height()
        self.splitter.setSizes([int(total_height * 0.1), int(total_height * 0.9)])

    def expand_config_section(self):
        """설정 섹션을 펼침 (작업 완료 시)"""
        self.config_container.setVisible(True)
        self.btn_collapse.setText("🔽 설정 접기")

        total_height = self.splitter.height()
        self.splitter.setSizes([int(total_height * 0.6), int(total_height * 0.4)])
