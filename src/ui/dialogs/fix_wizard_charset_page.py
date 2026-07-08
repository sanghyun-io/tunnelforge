"""
마이그레이션 수정 위저드 2단계: 문자셋 이슈 테이블 선택 (FK 안전 변경) 페이지
"""
from PyQt6.QtWidgets import (
    QCheckBox, QFrame, QGroupBox, QHBoxLayout, QLabel, QMessageBox,
    QPushButton, QScrollArea, QVBoxLayout, QWidget, QWizardPage
)
from PyQt6.QtCore import Qt
from typing import Dict, List, Set

from src.core.migration_fix_wizard import CharsetTableInfo, FKSafeCharsetChanger


class CharsetFixPage(QWizardPage):
    """2단계: 문자셋 변경 대상 테이블 선택

    FK 안전 변경 방식으로 일괄 처리합니다.
    - 모든 테이블이 기본 선택됨
    - 체크 해제 시 = 건너뛰기
    - 건너뛰기 시 FK 연쇄 영향 확인 다이얼로그 표시
    """

    def __init__(self, wizard: "FixWizardDialog"):
        super().__init__(wizard)
        self.wizard_dialog = wizard

        self.setTitle("문자셋 변경 대상 테이블")
        self.setSubTitle("FK 안전 변경 방식으로 일괄 처리됩니다. (FK DROP → charset 변경 → FK 재생성)")

        self.table_checkboxes: Dict[str, QCheckBox] = {}
        self.table_infos: List[CharsetTableInfo] = []
        self._updating_checkboxes = False  # 연쇄 업데이트 중 플래그
        self._fk_cache: List = []  # 전체 테이블 대상 FK 조회 결과 (1회 캐시, UI 스레드 DB 재조회 방지)

        self.init_ui()

    def init_ui(self):
        layout = QVBoxLayout(self)

        # 안내 텍스트
        info_frame = QFrame()
        info_frame.setStyleSheet("""
            QFrame {
                background-color: #e8f4fd;
                border: 1px solid #90caf9;
                border-radius: 4px;
                padding: 10px;
            }
        """)
        info_layout = QVBoxLayout(info_frame)

        info_label = QLabel(
            "ℹ️ <b>FK 안전 변경 방식</b>으로 모든 테이블이 일괄 처리됩니다.<br>"
            "체크 해제 시 해당 테이블을 건너뜁니다.<br>"
            "FK 관계로 인해 연쇄적으로 건너뛰어야 하는 테이블이 있을 수 있습니다."
        )
        info_label.setWordWrap(True)
        info_layout.addWidget(info_label)

        layout.addWidget(info_frame)

        # 테이블 목록 영역
        self.table_group = QGroupBox("대상 테이블")
        table_layout = QVBoxLayout(self.table_group)

        # 스크롤 영역
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setMinimumHeight(300)

        self.scroll_content = QWidget()
        self.scroll_layout = QVBoxLayout(self.scroll_content)
        self.scroll_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        scroll_area.setWidget(self.scroll_content)
        table_layout.addWidget(scroll_area)

        layout.addWidget(self.table_group)

        # 통계 라벨
        stats_layout = QHBoxLayout()
        self.lbl_stats = QLabel("선택됨: 0개 | 건너뛰기: 0개 | 총 FK: 0개")
        self.lbl_stats.setStyleSheet("font-weight: bold; color: #333;")
        stats_layout.addWidget(self.lbl_stats)
        stats_layout.addStretch()
        layout.addLayout(stats_layout)

        # 버튼
        btn_layout = QHBoxLayout()

        btn_select_all = QPushButton("전체 선택")
        btn_select_all.clicked.connect(self.select_all)

        btn_deselect_all = QPushButton("전체 해제")
        btn_deselect_all.clicked.connect(self.deselect_all)

        btn_layout.addWidget(btn_select_all)
        btn_layout.addWidget(btn_deselect_all)
        btn_layout.addStretch()

        layout.addLayout(btn_layout)

    def initializePage(self):
        """페이지 초기화"""
        # 문자셋 이슈가 없으면 이 페이지를 건너뛴다 (nextId()가 다음 페이지로 넘김).
        # 이전에 문자셋 이슈를 선택했다가 뒤로 가서 선택을 해제한 경우를 대비해
        # 이전 실행에서 남은 테이블/FK 상태를 반드시 초기화한다.
        # 그렇지 않으면 validatePage()가 stale table_infos를 그대로 읽어
        # wizard_dialog.charset_tables_to_fix에 이미 취소된 테이블이 남고,
        # PreviewPage가 더 이상 유효하지 않은 ALTER TABLE SQL을 노출하게 된다.
        if not self.wizard_dialog.has_charset_issues():
            self._clear_table_widgets()
            self.table_infos = []
            self._fk_cache = []
            self.wizard_dialog.charset_tables_to_fix = set()
            self.update_stats()
            return

        self._clear_table_widgets()

        # 테이블 목록 빌드
        plan_builder = self.wizard_dialog.charset_plan_builder
        if not plan_builder:
            return

        self.table_infos = plan_builder.build_full_table_list()

        # FK 관계를 전체 테이블 집합 기준으로 1회만 조회하여 캐시한다.
        # (체크박스를 토글할 때마다 update_stats()에서 매번 DB를 재조회하면
        #  UI 스레드가 매 클릭마다 블로킹된다.)
        all_tables = {info.table_name for info in self.table_infos}
        if all_tables:
            changer = FKSafeCharsetChanger(
                self.wizard_dialog.connector,
                self.wizard_dialog.schema
            )
            self._fk_cache = changer.get_related_fks(all_tables)
        else:
            self._fk_cache = []

        # 테이블별 체크박스 생성
        for info in self.table_infos:
            widget = self._create_table_widget(info)
            self.scroll_layout.addWidget(widget)

        self.update_stats()

    def _clear_table_widgets(self):
        """테이블 체크박스 위젯 전체 제거"""
        for i in reversed(range(self.scroll_layout.count())):
            widget = self.scroll_layout.itemAt(i).widget()
            if widget:
                widget.deleteLater()
        self.table_checkboxes.clear()

    def _create_table_widget(self, info: CharsetTableInfo) -> QWidget:
        """테이블 위젯 생성"""
        widget = QFrame()
        widget.setStyleSheet("""
            QFrame {
                background-color: #fafafa;
                border: 1px solid #e0e0e0;
                border-radius: 4px;
                padding: 8px;
                margin: 2px;
            }
            QFrame:hover {
                background-color: #f0f0f0;
            }
        """)
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(8, 8, 8, 8)

        # 첫 번째 줄: 체크박스 + 테이블명 + 태그
        header_layout = QHBoxLayout()

        chk = QCheckBox()
        chk.setChecked(not info.skip)
        chk.stateChanged.connect(lambda state, t=info.table_name: self.on_table_check_changed(t, state))
        self.table_checkboxes[info.table_name] = chk
        header_layout.addWidget(chk)

        # 테이블명
        lbl_name = QLabel(f"<b>{info.table_name}</b>")
        header_layout.addWidget(lbl_name)

        # 태그: 원본 이슈 / FK 연관
        if info.is_original_issue:
            tag = QLabel("원본 이슈")
            tag.setStyleSheet("""
                QLabel {
                    background-color: #e74c3c;
                    color: white;
                    padding: 2px 6px;
                    border-radius: 3px;
                    font-size: 10px;
                }
            """)
        else:
            tag = QLabel("FK 연관")
            tag.setStyleSheet("""
                QLabel {
                    background-color: #3498db;
                    color: white;
                    padding: 2px 6px;
                    border-radius: 3px;
                    font-size: 10px;
                }
            """)
        header_layout.addWidget(tag)
        header_layout.addStretch()

        # 현재 charset
        lbl_charset = QLabel(f"{info.current_charset} / {info.current_collation}")
        lbl_charset.setStyleSheet("color: #666; font-size: 11px;")
        header_layout.addWidget(lbl_charset)

        layout.addLayout(header_layout)

        # 두 번째 줄: FK 관계
        if info.fk_parents or info.fk_children:
            fk_layout = QHBoxLayout()
            fk_layout.setContentsMargins(24, 0, 0, 0)

            fk_parts = []
            if info.fk_parents:
                fk_parts.append(f"부모: {', '.join(info.fk_parents)}")
            if info.fk_children:
                fk_parts.append(f"자식: {', '.join(info.fk_children)}")

            lbl_fk = QLabel("└─ FK: " + " | ".join(fk_parts))
            lbl_fk.setStyleSheet("color: #888; font-size: 10px;")
            fk_layout.addWidget(lbl_fk)
            fk_layout.addStretch()

            layout.addLayout(fk_layout)

        return widget

    def on_table_check_changed(self, table_name: str, state: int):
        """테이블 체크 상태 변경"""
        if self._updating_checkboxes:
            return

        is_checked = (state == Qt.CheckState.Checked.value)

        if not is_checked:
            # 건너뛰기 선택 → 연쇄 확인
            self._handle_skip_table(table_name)
        else:
            # 선택 복원
            self._handle_restore_table(table_name)

        self.update_stats()
        self.completeChanged.emit()

    def _handle_skip_table(self, table_name: str):
        """테이블 건너뛰기 처리"""
        plan_builder = self.wizard_dialog.charset_plan_builder
        if not plan_builder:
            return

        # 연쇄 건너뛰기 테이블 계산
        cascade_tables = plan_builder.get_cascade_skip_tables(table_name)

        if cascade_tables:
            # 확인 다이얼로그
            cascade_list = '\n'.join(f"• {t}" for t in sorted(cascade_tables))
            reply = QMessageBox.question(
                self,
                "연쇄 건너뛰기 확인",
                f"'{table_name}' 테이블을 건너뛰면\n"
                f"FK 관계로 인해 다음 테이블도 함께 건너뛰어야 합니다:\n\n"
                f"{cascade_list}\n\n"
                f"진행하시겠습니까?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No
            )

            if reply == QMessageBox.StandardButton.Yes:
                # 연쇄 테이블도 함께 건너뛰기
                self._skip_tables({table_name} | cascade_tables)
            else:
                # 체크박스 복원
                self._restore_checkbox(table_name)
        else:
            # 연쇄 영향 없음 → 바로 건너뛰기
            self._skip_tables({table_name})

    def _handle_restore_table(self, table_name: str):
        """테이블 복원 처리 (건너뛰기 해제)"""
        # table_infos에서 해당 테이블 찾아서 skip 해제
        for info in self.table_infos:
            if info.table_name == table_name:
                info.skip = False
                break

    def _skip_tables(self, tables: Set[str]):
        """테이블들을 건너뛰기 처리"""
        self._updating_checkboxes = True
        try:
            for table in tables:
                # table_infos 업데이트
                for info in self.table_infos:
                    if info.table_name == table:
                        info.skip = True
                        break

                # 체크박스 업데이트
                if table in self.table_checkboxes:
                    self.table_checkboxes[table].setChecked(False)
        finally:
            self._updating_checkboxes = False

    def _restore_checkbox(self, table_name: str):
        """체크박스 복원 (건너뛰기 취소)"""
        self._updating_checkboxes = True
        try:
            if table_name in self.table_checkboxes:
                self.table_checkboxes[table_name].setChecked(True)
        finally:
            self._updating_checkboxes = False

    def select_all(self):
        """전체 선택"""
        self._updating_checkboxes = True
        try:
            for info in self.table_infos:
                info.skip = False
            for chk in self.table_checkboxes.values():
                chk.setChecked(True)
        finally:
            self._updating_checkboxes = False
        self.update_stats()
        self.completeChanged.emit()

    def deselect_all(self):
        """전체 해제"""
        self._updating_checkboxes = True
        try:
            for info in self.table_infos:
                info.skip = True
            for chk in self.table_checkboxes.values():
                chk.setChecked(False)
        finally:
            self._updating_checkboxes = False
        self.update_stats()
        self.completeChanged.emit()

    def update_stats(self):
        """통계 업데이트"""
        total = len(self.table_infos)
        selected = sum(1 for info in self.table_infos if not info.skip)
        skipped = total - selected

        # FK 개수 계산 (initializePage()에서 전체 테이블 기준으로 1회 캐시한 결과를
        # 현재 선택된 부분집합으로 필터링만 한다 — 체크박스 토글마다 DB를 재조회하지 않는다)
        fk_count = 0
        if self.wizard_dialog.charset_plan_builder:
            tables_to_fix = {info.table_name for info in self.table_infos if not info.skip}
            if tables_to_fix:
                fk_count = sum(
                    1 for fk in self._fk_cache
                    if fk.table_name in tables_to_fix or fk.ref_table in tables_to_fix
                )

        self.lbl_stats.setText(f"선택됨: {selected}개 | 건너뛰기: {skipped}개 | 총 FK: {fk_count}개")

    def isComplete(self) -> bool:
        """다음 단계 진행 가능 여부"""
        # 문자셋 이슈가 없으면 무조건 통과
        if not self.wizard_dialog.has_charset_issues():
            return True

        # 최소 1개 테이블 선택 필요
        return any(not info.skip for info in self.table_infos)

    def nextId(self) -> int:
        """다음 페이지 결정

        다른 이슈가 없으면 FixOptionPage 건너뛰기
        """
        # 문자셋 이슈가 없으면 다음 페이지(FixOptionPage)로
        if not self.wizard_dialog.has_charset_issues():
            # 다른 이슈도 없으면 PreviewPage로
            if not self.wizard_dialog.has_other_issues():
                return self.wizard_dialog.preview_page_id
            return self.wizard_dialog.option_page_id

        # 다른 이슈가 없으면 PreviewPage로
        if not self.wizard_dialog.has_other_issues():
            return self.wizard_dialog.preview_page_id

        # 기본: 다음 페이지 (FixOptionPage)
        return self.wizard_dialog.option_page_id

    def validatePage(self) -> bool:
        """페이지 유효성 검사 및 데이터 저장"""
        # 선택된 테이블 저장
        tables_to_fix = {info.table_name for info in self.table_infos if not info.skip}
        self.wizard_dialog.charset_tables_to_fix = tables_to_fix

        return True
