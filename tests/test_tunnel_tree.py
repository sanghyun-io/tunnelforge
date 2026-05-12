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
