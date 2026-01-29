"""
터널 트리 위젯

폴더/카테고리 기반 터널 정리 및 드래그 앤 드롭 지원
"""

from PyQt6.QtWidgets import (
    QTreeWidget, QTreeWidgetItem, QHeaderView, QPushButton,
    QHBoxLayout, QWidget, QAbstractItemView, QMenu
)
from PyQt6.QtCore import Qt, pyqtSignal, QMimeData
from PyQt6.QtGui import QColor, QDrag

from src.ui.styles import ButtonStyles


class TunnelTreeWidget(QTreeWidget):
    """터널 그룹핑을 지원하는 트리 위젯"""

    # 시그널 정의
    tunnel_start_requested = pyqtSignal(dict)    # 터널 시작 요청
    tunnel_stop_requested = pyqtSignal(dict)     # 터널 중지 요청
    tunnel_edit_requested = pyqtSignal(dict)     # 터널 수정 요청
    tunnel_delete_requested = pyqtSignal(dict)   # 터널 삭제 요청
    tunnel_db_connect = pyqtSignal(dict)         # DB 연결 요청
    tunnel_sql_editor = pyqtSignal(dict)         # SQL 에디터 요청
    tunnel_export = pyqtSignal(dict)             # Export 요청
    tunnel_import = pyqtSignal(dict)             # Import 요청
    tunnel_test = pyqtSignal(dict)               # 연결 테스트 요청
    tunnel_duplicate = pyqtSignal(dict)          # 터널 복사 요청
    group_connect_all = pyqtSignal(str)          # 그룹 전체 연결
    group_disconnect_all = pyqtSignal(str)       # 그룹 전체 해제
    group_edit_requested = pyqtSignal(str)       # 그룹 수정 요청
    group_delete_requested = pyqtSignal(str)     # 그룹 삭제 요청
    tunnel_moved_to_group = pyqtSignal(str, str) # (tunnel_id, group_id 또는 None)

    # 아이템 타입 상수
    ITEM_TYPE_GROUP = 1
    ITEM_TYPE_TUNNEL = 2
    ITEM_TYPE_UNGROUPED_HEADER = 3

    def __init__(self, parent=None):
        super().__init__(parent)

        # 컬럼 설정
        self.setHeaderLabels([
            "상태", "이름", "로컬 포트", "타겟 호스트",
            "기본 스키마", "전원", "관리"
        ])

        # 열 너비 설정
        header = self.header()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(False)

        # 드래그 앤 드롭 설정
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)

        # 선택 및 확장 설정
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setExpandsOnDoubleClick(False)

        # 컨텍스트 메뉴
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_context_menu)

        # 더블클릭 이벤트
        self.itemDoubleClicked.connect(self._on_item_double_clicked)

        # 확장/축소 이벤트
        self.itemExpanded.connect(self._on_item_expanded)
        self.itemCollapsed.connect(self._on_item_collapsed)

        # 내부 데이터
        self._tunnel_items = {}  # tunnel_id -> QTreeWidgetItem
        self._group_items = {}   # group_id -> QTreeWidgetItem
        self._ungrouped_header = None

    def set_column_ratios(self, ratios: list):
        """컬럼 너비 비율 설정"""
        total_width = self.viewport().width()
        if total_width > 0:
            header = self.header()
            for i, ratio in enumerate(ratios):
                if i < header.count():
                    header.resizeSection(i, int(total_width * ratio))

    def load_data(self, tunnels: list, groups: list, ungrouped_order: list):
        """데이터 로드 및 트리 구성

        Args:
            tunnels: 터널 설정 목록
            groups: 그룹 목록
            ungrouped_order: 그룹에 속하지 않은 터널 ID 순서
        """
        self.clear()
        self._tunnel_items.clear()
        self._group_items.clear()
        self._ungrouped_header = None

        # 터널 ID -> 터널 데이터 맵
        tunnel_map = {t['id']: t for t in tunnels}

        # 그룹에 속한 터널 ID 집합
        grouped_tunnel_ids = set()
        for group in groups:
            grouped_tunnel_ids.update(group.get('tunnel_ids', []))

        # 그룹 아이템 생성
        for group in groups:
            group_item = self._create_group_item(group)
            self.addTopLevelItem(group_item)
            self._group_items[group['id']] = group_item

            # 그룹 내 터널 추가
            for tunnel_id in group.get('tunnel_ids', []):
                if tunnel_id in tunnel_map:
                    tunnel_item = self._create_tunnel_item(tunnel_map[tunnel_id])
                    group_item.addChild(tunnel_item)
                    self._tunnel_items[tunnel_id] = tunnel_item

            # 접힘 상태 복원
            if group.get('collapsed', False):
                group_item.setExpanded(False)
            else:
                group_item.setExpanded(True)

        # 그룹 없는 터널 처리
        ungrouped_tunnels = []

        # ungrouped_order에 있는 터널
        for tunnel_id in ungrouped_order:
            if tunnel_id in tunnel_map and tunnel_id not in grouped_tunnel_ids:
                ungrouped_tunnels.append(tunnel_map[tunnel_id])
                grouped_tunnel_ids.add(tunnel_id)

        # 어디에도 속하지 않은 터널 (새로 추가된 터널 등)
        for tunnel in tunnels:
            if tunnel['id'] not in grouped_tunnel_ids:
                ungrouped_tunnels.append(tunnel)

        # 그룹 없음 헤더 추가
        if ungrouped_tunnels:
            self._ungrouped_header = self._create_ungrouped_header()
            self.addTopLevelItem(self._ungrouped_header)

            for tunnel in ungrouped_tunnels:
                tunnel_item = self._create_tunnel_item(tunnel)
                self._ungrouped_header.addChild(tunnel_item)
                self._tunnel_items[tunnel['id']] = tunnel_item

            self._ungrouped_header.setExpanded(True)

    def _create_group_item(self, group: dict) -> QTreeWidgetItem:
        """그룹 아이템 생성"""
        item = QTreeWidgetItem()
        item.setData(0, Qt.ItemDataRole.UserRole, {
            'type': self.ITEM_TYPE_GROUP,
            'id': group['id'],
            'data': group
        })

        # 그룹 이름 및 색상
        color = group.get('color', '#3498db')
        tunnel_count = len(group.get('tunnel_ids', []))
        item.setText(1, f"📁 {group['name']} ({tunnel_count})")

        # 색상 표시
        item.setForeground(1, QColor(color))

        # 첫 번째 열에 색상 마커
        item.setText(0, "●")
        item.setForeground(0, QColor(color))

        # 그룹 행은 드래그 불가, 드롭만 가능
        item.setFlags(
            item.flags() |
            Qt.ItemFlag.ItemIsDropEnabled
        )
        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsDragEnabled)

        return item

    def _create_ungrouped_header(self) -> QTreeWidgetItem:
        """그룹 없음 헤더 생성"""
        item = QTreeWidgetItem()
        item.setData(0, Qt.ItemDataRole.UserRole, {
            'type': self.ITEM_TYPE_UNGROUPED_HEADER
        })
        item.setText(0, "─")
        item.setText(1, "📋 그룹 없음")
        item.setForeground(1, QColor("#7f8c8d"))

        # 드래그 불가, 드롭 가능
        item.setFlags(
            item.flags() |
            Qt.ItemFlag.ItemIsDropEnabled
        )
        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsDragEnabled)

        return item

    def _create_tunnel_item(self, tunnel: dict) -> QTreeWidgetItem:
        """터널 아이템 생성"""
        item = QTreeWidgetItem()
        item.setData(0, Qt.ItemDataRole.UserRole, {
            'type': self.ITEM_TYPE_TUNNEL,
            'id': tunnel['id'],
            'data': tunnel
        })

        # 상태 아이콘 (초기값: 연결 안됨)
        item.setText(0, "⚪")

        # 이름
        item.setText(1, tunnel.get('name', ''))

        # 로컬 포트 (connection_mode에 따라 다름)
        if tunnel.get('connection_mode') == 'direct':
            item.setText(2, "-")
        else:
            item.setText(2, str(tunnel.get('local_port', '')))

        # 타겟 호스트
        remote_host = tunnel.get('remote_host', '')
        remote_port = tunnel.get('remote_port', '')
        item.setText(3, f"{remote_host}:{remote_port}")

        # 기본 스키마
        item.setText(4, tunnel.get('default_schema', '-') or '-')

        # 드래그 가능
        item.setFlags(
            item.flags() |
            Qt.ItemFlag.ItemIsDragEnabled |
            Qt.ItemFlag.ItemIsSelectable
        )
        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsDropEnabled)

        return item

    def update_tunnel_status(self, tunnel_id: str, is_running: bool):
        """터널 상태 업데이트"""
        if tunnel_id in self._tunnel_items:
            item = self._tunnel_items[tunnel_id]
            if is_running:
                item.setText(0, "🟢")
            else:
                item.setText(0, "⚪")

    def set_tunnel_buttons(self, tunnel_id: str, button_widget: QWidget):
        """터널 아이템에 버튼 위젯 설정"""
        if tunnel_id in self._tunnel_items:
            item = self._tunnel_items[tunnel_id]
            # 전원 버튼 (컬럼 5)
            # 관리 버튼 (컬럼 6)
            self.setItemWidget(item, 6, button_widget)

    def set_power_button(self, tunnel_id: str, button: QPushButton):
        """전원 버튼 설정"""
        if tunnel_id in self._tunnel_items:
            item = self._tunnel_items[tunnel_id]
            self.setItemWidget(item, 5, button)

    def _show_context_menu(self, pos):
        """컨텍스트 메뉴 표시"""
        item = self.itemAt(pos)
        if not item:
            return

        item_data = item.data(0, Qt.ItemDataRole.UserRole)
        if not item_data:
            return

        menu = QMenu(self)
        item_type = item_data.get('type')

        if item_type == self.ITEM_TYPE_GROUP:
            # 그룹 컨텍스트 메뉴
            group_id = item_data.get('id')

            action_connect = menu.addAction("🔗 모두 연결")
            action_connect.triggered.connect(lambda: self.group_connect_all.emit(group_id))

            action_disconnect = menu.addAction("⛔ 모두 해제")
            action_disconnect.triggered.connect(lambda: self.group_disconnect_all.emit(group_id))

            menu.addSeparator()

            action_edit = menu.addAction("✏️ 그룹 수정")
            action_edit.triggered.connect(lambda: self.group_edit_requested.emit(group_id))

            action_delete = menu.addAction("🗑️ 그룹 삭제")
            action_delete.triggered.connect(lambda: self.group_delete_requested.emit(group_id))

        elif item_type == self.ITEM_TYPE_TUNNEL:
            # 터널 컨텍스트 메뉴
            tunnel_data = item_data.get('data', {})

            action_duplicate = menu.addAction("📋 복사하여 새로 만들기")
            action_duplicate.triggered.connect(lambda: self.tunnel_duplicate.emit(tunnel_data))

            action_edit = menu.addAction("✏️ 수정")
            action_edit.triggered.connect(lambda: self.tunnel_edit_requested.emit(tunnel_data))

            action_test = menu.addAction("🔍 연결 테스트")
            action_test.triggered.connect(lambda: self.tunnel_test.emit(tunnel_data))

            menu.addSeparator()

            action_db = menu.addAction("🔌 DB 연결")
            action_db.triggered.connect(lambda: self.tunnel_db_connect.emit(tunnel_data))

            action_sql = menu.addAction("📝 SQL 에디터")
            action_sql.triggered.connect(lambda: self.tunnel_sql_editor.emit(tunnel_data))

            menu.addSeparator()

            action_export = menu.addAction("📤 Export")
            action_export.triggered.connect(lambda: self.tunnel_export.emit(tunnel_data))

            action_import = menu.addAction("📥 Import")
            action_import.triggered.connect(lambda: self.tunnel_import.emit(tunnel_data))

            menu.addSeparator()

            action_delete = menu.addAction("🗑️ 삭제")
            action_delete.triggered.connect(lambda: self.tunnel_delete_requested.emit(tunnel_data))

        menu.exec(self.mapToGlobal(pos))

    def _on_item_double_clicked(self, item, column):
        """더블클릭 이벤트"""
        item_data = item.data(0, Qt.ItemDataRole.UserRole)
        if not item_data:
            return

        item_type = item_data.get('type')

        if item_type == self.ITEM_TYPE_GROUP:
            # 그룹 더블클릭: 접기/펼치기
            item.setExpanded(not item.isExpanded())
        elif item_type == self.ITEM_TYPE_TUNNEL:
            # 터널 더블클릭: 수정 다이얼로그
            tunnel_data = item_data.get('data', {})
            self.tunnel_edit_requested.emit(tunnel_data)

    def _on_item_expanded(self, item):
        """아이템 확장됨"""
        item_data = item.data(0, Qt.ItemDataRole.UserRole)
        if item_data and item_data.get('type') == self.ITEM_TYPE_GROUP:
            group_id = item_data.get('id')
            # collapsed 상태 저장 (False)
            self._save_collapsed_state(group_id, False)

    def _on_item_collapsed(self, item):
        """아이템 축소됨"""
        item_data = item.data(0, Qt.ItemDataRole.UserRole)
        if item_data and item_data.get('type') == self.ITEM_TYPE_GROUP:
            group_id = item_data.get('id')
            # collapsed 상태 저장 (True)
            self._save_collapsed_state(group_id, True)

    def _save_collapsed_state(self, group_id: str, collapsed: bool):
        """접힘 상태 저장 (config_manager 연동)"""
        # 부모 윈도우에서 config_manager 접근
        parent = self.parent()
        while parent:
            if hasattr(parent, 'config_mgr'):
                parent.config_mgr.save_group_collapsed_state(group_id, collapsed)
                break
            parent = parent.parent()

    def dropEvent(self, event):
        """드롭 이벤트 처리"""
        source_item = self.currentItem()
        if not source_item:
            event.ignore()
            return

        source_data = source_item.data(0, Qt.ItemDataRole.UserRole)
        if not source_data or source_data.get('type') != self.ITEM_TYPE_TUNNEL:
            event.ignore()
            return

        # 드롭 대상 아이템 찾기
        target_item = self.itemAt(event.position().toPoint())
        if not target_item:
            event.ignore()
            return

        target_data = target_item.data(0, Qt.ItemDataRole.UserRole)
        if not target_data:
            event.ignore()
            return

        tunnel_id = source_data.get('id')
        target_type = target_data.get('type')

        # 대상 그룹 결정
        target_group_id = None
        if target_type == self.ITEM_TYPE_GROUP:
            target_group_id = target_data.get('id')
        elif target_type == self.ITEM_TYPE_UNGROUPED_HEADER:
            target_group_id = None
        elif target_type == self.ITEM_TYPE_TUNNEL:
            # 터널 위에 드롭 -> 해당 터널의 부모 그룹으로 이동
            parent = target_item.parent()
            if parent:
                parent_data = parent.data(0, Qt.ItemDataRole.UserRole)
                if parent_data and parent_data.get('type') == self.ITEM_TYPE_GROUP:
                    target_group_id = parent_data.get('id')
            else:
                target_group_id = None
        else:
            event.ignore()
            return

        # 시그널 발생
        self.tunnel_moved_to_group.emit(tunnel_id, target_group_id or "")

        event.accept()

    def dragEnterEvent(self, event):
        """드래그 진입 이벤트"""
        if event.source() == self:
            event.accept()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        """드래그 이동 이벤트"""
        target_item = self.itemAt(event.position().toPoint())
        if target_item:
            target_data = target_item.data(0, Qt.ItemDataRole.UserRole)
            if target_data:
                target_type = target_data.get('type')
                if target_type in (self.ITEM_TYPE_GROUP, self.ITEM_TYPE_UNGROUPED_HEADER, self.ITEM_TYPE_TUNNEL):
                    event.accept()
                    return
        event.ignore()
