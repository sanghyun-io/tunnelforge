import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QPushButton, QWidget

from src.ui.widgets.tunnel_tree import TunnelTreeWidget


app = QApplication.instance() or QApplication(sys.argv)


def sample_tunnel(tunnel_id="tunnel-1"):
    return {
        "id": tunnel_id,
        "name": "테스트 터널",
        "connection_mode": "direct",
        "remote_host": "127.0.0.1",
        "remote_port": 3306,
        "default_schema": "test",
    }


def test_load_data_removes_previous_item_widgets():
    tree = TunnelTreeWidget()
    try:
        tunnel = sample_tunnel()
        tree.load_data([tunnel], [], [])
        tree.set_power_button(tunnel["id"], QPushButton("시작"))
        tree.set_tunnel_buttons(tunnel["id"], QWidget())

        item = tree._tunnel_items[tunnel["id"]]
        old_power = tree.itemWidget(item, 5)
        old_actions = tree.itemWidget(item, 6)
        assert old_power is not None
        assert old_actions is not None

        tree.load_data([tunnel], [], [])
        new_item = tree._tunnel_items[tunnel["id"]]

        assert tree.itemWidget(new_item, 5) is None
        assert tree.itemWidget(new_item, 6) is None
        assert old_power.parent() is None
        assert old_actions.parent() is None
    finally:
        tree.close()


def test_tunnel_tree_no_unused_column_ratio_api():
    assert not hasattr(TunnelTreeWidget, "set_column_ratios")


def test_tunnel_tree_exposes_orphan_check_signal():
    # 고아 레코드 분석 진입점(트리 컨텍스트 메뉴 시그널)이 복원되어 있어야 한다.
    assert hasattr(TunnelTreeWidget, "tunnel_orphan_check")


def test_context_menu_wires_orphan_check_action():
    # 컨텍스트 메뉴 액션이 tunnel_orphan_check 시그널을 emit하도록 연결됐는지
    # 소스 레벨로 검증(오프스크린에서 실제 메뉴를 띄우지 않음).
    import inspect

    source = inspect.getsource(TunnelTreeWidget._build_tunnel_context_menu)
    assert "고아 레코드 분석" in source
    assert "self.tunnel_orphan_check.emit" in source


def test_group_expand_collapse_emits_collapsed_state():
    tree = TunnelTreeWidget()
    try:
        group = {"id": "group-1", "name": "Group", "color": "#3498db", "tunnel_ids": []}
        tree.load_data([], [group], [])
        item = tree._group_items["group-1"]
        emitted = []
        tree.group_collapsed_changed.connect(lambda group_id, collapsed: emitted.append((group_id, collapsed)))

        tree._on_item_collapsed(item)
        tree._on_item_expanded(item)

        assert emitted == [("group-1", True), ("group-1", False)]
    finally:
        tree.close()


def test_update_tunnel_status_toggles_icon_without_reload():
    tree = TunnelTreeWidget()
    try:
        tunnel = sample_tunnel()
        tree.load_data([tunnel], [], [])
        item = tree._tunnel_items[tunnel["id"]]

        tree.update_tunnel_status(tunnel["id"], True)
        assert item.text(0) == "🟢"

        tree.update_tunnel_status(tunnel["id"], False)
        assert item.text(0) == "⚪"
    finally:
        tree.close()


def test_update_tunnel_status_shows_reconnecting_and_error_with_tooltip():
    tree = TunnelTreeWidget()
    try:
        tunnel = sample_tunnel()
        tree.load_data([tunnel], [], [])
        item = tree._tunnel_items[tunnel["id"]]

        tree.update_tunnel_status(tunnel["id"], False, "reconnecting", "재연결 중 (2/5)")
        assert item.text(0) == "🟡"
        assert item.toolTip(0) == "재연결 중 (2/5)"

        tree.update_tunnel_status(tunnel["id"], False, "error", "연결 실패: timeout")
        assert item.text(0) == "🔴"
        assert item.toolTip(0) == "연결 실패: timeout"

        tree.update_tunnel_status(tunnel["id"], True)
        assert item.text(0) == "🟢"
        assert item.toolTip(0) == ""
    finally:
        tree.close()


def test_empty_state_hint_only_when_no_tunnels():
    tree = TunnelTreeWidget()
    try:
        tree.load_data([], [], [])
        assert not tree._empty_label.isHidden()
        assert "연결 추가" in tree._empty_label.text() or "Add Connection" in tree._empty_label.text()

        tree.load_data([sample_tunnel()], [], [])
        assert tree._empty_label.isHidden()
    finally:
        tree.close()


def test_move_to_group_menu_moves_without_drag_and_drop():
    from PyQt6.QtWidgets import QMenu

    tree = TunnelTreeWidget()
    try:
        tunnel = sample_tunnel()
        groups = [{"id": "g1", "name": "운영", "tunnel_ids": [tunnel["id"]]}, {"id": "g2", "name": "개발", "tunnel_ids": []}]
        tree.load_data([tunnel], groups, [])
        assert tree.tunnel_group_id(tunnel["id"]) == "g1"
        moved, new_group = [], []
        tree.tunnel_moved_to_group.connect(lambda tunnel_id, group_id: moved.append((tunnel_id, group_id)))
        tree.tunnel_move_to_new_group.connect(new_group.append)

        menu = QMenu()
        tree._build_move_to_group_menu(menu, tunnel["id"])
        submenu = menu.actions()[0].menu()
        actions = {action.text(): action for action in submenu.actions() if action.text()}
        assert actions["운영"].isChecked() and not actions["운영"].isEnabled()
        actions["개발"].trigger()
        [remove] = [action for text, action in actions.items() if "그룹에서 빼기" in text or "Remove" in text]
        assert remove.isEnabled()
        remove.trigger()
        [create] = [action for text, action in actions.items() if text.startswith("➕")]
        create.trigger()
        assert moved == [(tunnel["id"], "g2"), (tunnel["id"], "")]
        assert new_group == [tunnel["id"]]
    finally:
        tree.deleteLater()
