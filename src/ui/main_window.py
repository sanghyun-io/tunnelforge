"""메인 UI 윈도우"""
import sys
import os
from PyQt6.QtWidgets import (QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
                             QTableWidget, QTableWidgetItem, QPushButton,
                             QLabel, QMessageBox, QHeaderView, QSystemTrayIcon, QMenu)
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QAction, QIcon


def get_resource_path(relative_path):
    """PyInstaller 빌드 환경에서 리소스 경로를 올바르게 반환"""
    if hasattr(sys, '_MEIPASS'):
        # PyInstaller로 빌드된 경우
        return os.path.join(sys._MEIPASS, relative_path)
    # 개발 환경
    return os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), relative_path)


from src.ui.dialogs.tunnel_config import TunnelConfigDialog
from src.ui.dialogs.settings import CloseConfirmDialog, SettingsDialog
from src.ui.dialogs.db_dialogs import MySQLShellWizard
from src.ui.dialogs.migration_dialogs import MigrationWizard
from src.ui.dialogs.test_dialogs import SQLExecutionDialog
from src.ui.dialogs.sql_editor_dialog import SQLEditorDialog


class StartupUpdateCheckerThread(QThread):
    """앱 시작 시 업데이트 확인 백그라운드 스레드"""
    update_available = pyqtSignal(str, str)  # latest_version, download_url

    def run(self):
        try:
            from src.core.update_checker import UpdateChecker
            checker = UpdateChecker()
            needs_update, latest_version, download_url, error_msg = checker.check_update()

            if needs_update and latest_version and download_url:
                self.update_available.emit(latest_version, download_url)
        except Exception:
            # 업데이트 확인 실패는 조용히 무시 (앱 실행에 영향 없음)
            pass


class TunnelManagerUI(QMainWindow):
    def __init__(self, config_manager, tunnel_engine):
        print("🖥️ UI 초기화 시작...")  # 디버깅용 로그
        super().__init__()
        self.config_mgr = config_manager
        self.engine = tunnel_engine

        # 설정 로드
        self.config_data = self.config_mgr.load_config()
        self.tunnels = self.config_data.get('tunnels', [])

        self._update_checker_thread = None

        self.init_ui()
        self.init_tray()
        self._check_update_on_startup()
        self._auto_connect_tunnels()
        print("✅ UI 초기화 완료")

    def init_ui(self):
        self.setWindowTitle("TunnelDB Manager")
        self.setGeometry(100, 100, 950, 600)

        # 창 아이콘 설정
        icon_path = get_resource_path('assets/icon.ico')
        if os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))

        # 메인 위젯 설정
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        layout = QVBoxLayout(central_widget)

        # --- 상단 헤더 ---
        header_layout = QHBoxLayout()
        title = QLabel("📡 터널링 연결 목록")
        title.setStyleSheet("font-size: 20px; font-weight: bold; color: #333;")

        # [새로고침] 버튼 - Secondary 스타일
        btn_refresh = QPushButton("🔄 설정 로드")
        btn_refresh.setStyleSheet("""
            QPushButton {
                background-color: #ecf0f1; color: #2c3e50;
                padding: 6px 16px; border-radius: 4px; border: 1px solid #bdc3c7;
            }
            QPushButton:hover { background-color: #d5dbdb; }
        """)
        btn_refresh.clicked.connect(self.reload_config)

        # [설정] 버튼 - Secondary 스타일
        btn_settings = QPushButton("⚙️ 설정")
        btn_settings.setStyleSheet("""
            QPushButton {
                background-color: #ecf0f1; color: #2c3e50;
                padding: 6px 16px; border-radius: 4px; border: 1px solid #bdc3c7;
            }
            QPushButton:hover { background-color: #d5dbdb; }
        """)
        btn_settings.clicked.connect(self.open_settings_dialog)

        # [연결 추가] 버튼 - Primary 스타일
        btn_add_tunnel = QPushButton("➕ 연결 추가")
        btn_add_tunnel.setStyleSheet("""
            QPushButton {
                background-color: #3498db; color: white; font-weight: bold;
                padding: 6px 16px; border-radius: 4px; border: none;
            }
            QPushButton:hover { background-color: #2980b9; }
        """)
        btn_add_tunnel.clicked.connect(self.add_tunnel_dialog)

        header_layout.addWidget(title)
        header_layout.addStretch()
        header_layout.addWidget(btn_add_tunnel)
        header_layout.addWidget(btn_refresh)
        header_layout.addWidget(btn_settings)
        layout.addLayout(header_layout)

        # --- 테이블 설정 ---
        self.table = QTableWidget()
        # 컬럼: 상태, 이름, 로컬포트, 타겟호스트, 기본 스키마, 전원, 관리(수정/삭제)
        self.table.setColumnCount(7)
        self.table.setHorizontalHeaderLabels(["상태", "이름", "로컬 포트", "타겟 호스트", "기본 스키마", "전원", "관리"])

        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)  # 이름 늘리기
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)  # 호스트 늘리기
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)  # 셀 수정 방지
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)  # 행 단위 선택

        # 컨텍스트 메뉴 설정
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.show_context_menu)

        layout.addWidget(self.table)

        # 하단 상태바
        self.statusBar().showMessage("준비됨")

        self.refresh_table()

    def init_tray(self):
        """시스템 트레이 아이콘 설정"""
        self.tray_icon = QSystemTrayIcon(self)
        # 커스텀 아이콘 사용 (PyInstaller 빌드 환경 지원)
        icon_path = get_resource_path('assets/icon.ico')
        if os.path.exists(icon_path):
            self.tray_icon.setIcon(QIcon(icon_path))

        tray_menu = QMenu()
        show_action = QAction("열기", self)
        show_action.triggered.connect(self.show)
        quit_action = QAction("종료", self)
        quit_action.triggered.connect(self.close_app)

        tray_menu.addAction(show_action)
        tray_menu.addAction(quit_action)

        self.tray_icon.setContextMenu(tray_menu)
        self.tray_icon.activated.connect(self._on_tray_activated)
        self.tray_icon.show()

    def _on_tray_activated(self, reason):
        """트레이 아이콘 클릭 시"""
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.show()
            self.activateWindow()

    def refresh_table(self):
        """설정 데이터와 현재 터널 상태를 기반으로 테이블을 갱신합니다."""
        self.table.setRowCount(0)

        for idx, tunnel in enumerate(self.tunnels):
            self.table.insertRow(idx)

            # config.json이 비어있거나 id가 없을 경우 대비
            tid = tunnel.get('id')
            if not tid:
                continue

            is_active = self.engine.is_running(tid)
            is_direct = tunnel.get('connection_mode') == 'direct'

            # 1. 상태 아이콘
            status_item = QTableWidgetItem("🟢" if is_active else "⚪")
            status_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(idx, 0, status_item)

            # 2. 이름 (직접 연결일 경우 표시 추가)
            name = tunnel.get('name', 'Unknown')
            if is_direct:
                name += " [직접]"
            self.table.setItem(idx, 1, QTableWidgetItem(name))

            # 3. 로컬 포트 (직접 연결일 경우 "-" 표시)
            if is_direct:
                port_str = "-"
            else:
                port_str = str(tunnel.get('local_port', ''))
            port_item = QTableWidgetItem(port_str)
            port_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(idx, 2, port_item)

            # 4. 타겟 호스트
            target_str = f"{tunnel.get('remote_host', '')}:{tunnel.get('remote_port', '')}"
            self.table.setItem(idx, 3, QTableWidgetItem(target_str))

            # 5. 기본 스키마
            schema_item = QTableWidgetItem(tunnel.get('default_schema') or '-')
            schema_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(idx, 4, schema_item)

            # 6. 전원 (Start/Stop) 버튼
            btn_power = QPushButton("중지" if is_active else "시작")
            if is_active:
                btn_power.setStyleSheet("""
                    QPushButton {
                        background-color: #e74c3c; color: white; font-weight: bold;
                        padding: 4px 12px; border-radius: 4px; border: none;
                    }
                    QPushButton:hover { background-color: #c0392b; }
                """)
                btn_power.clicked.connect(lambda checked, t=tunnel: self.stop_tunnel(t))
            else:
                btn_power.setStyleSheet("""
                    QPushButton {
                        background-color: #2ecc71; color: white; font-weight: bold;
                        padding: 4px 12px; border-radius: 4px; border: none;
                    }
                    QPushButton:hover { background-color: #27ae60; }
                """)
                btn_power.clicked.connect(lambda checked, t=tunnel: self.start_tunnel(t))
            self.table.setCellWidget(idx, 5, btn_power)

            # 7. 관리 (수정/삭제) 버튼 그룹
            container = QWidget()
            h_box = QHBoxLayout(container)
            h_box.setContentsMargins(4, 4, 4, 4)
            h_box.setSpacing(5)

            btn_edit = QPushButton("수정")
            btn_edit.setStyleSheet("""
                QPushButton {
                    background-color: #ecf0f1; color: #2c3e50;
                    padding: 4px 10px; border-radius: 4px; border: 1px solid #bdc3c7;
                }
                QPushButton:hover { background-color: #d5dbdb; }
            """)
            btn_edit.clicked.connect(lambda checked, t=tunnel: self.edit_tunnel_dialog(t))
            h_box.addWidget(btn_edit)

            btn_del = QPushButton("삭제")
            btn_del.setStyleSheet("""
                QPushButton {
                    background-color: #fadbd8; color: #c0392b;
                    padding: 4px 10px; border-radius: 4px; border: 1px solid #e74c3c;
                }
                QPushButton:hover { background-color: #f5b7b1; }
            """)
            btn_del.clicked.connect(lambda checked, t=tunnel: self.delete_tunnel(t))
            h_box.addWidget(btn_del)

            self.table.setCellWidget(idx, 6, container)

    # --- 기능 로직 ---
    def add_tunnel_dialog(self):
        """연결 추가 팝업"""
        # 수정됨: self.engine 전달
        dialog = TunnelConfigDialog(self, tunnel_engine=self.engine)
        if dialog.exec():
            new_data = dialog.get_data()
            new_data = self._process_credentials(new_data)
            self.tunnels.append(new_data)
            self.save_and_refresh()

    def edit_tunnel_dialog(self, tunnel):
        """연결 수정 팝업"""
        if self.engine.is_running(tunnel['id']):
            QMessageBox.warning(self, "수정 불가", "실행 중인 터널은 수정할 수 없습니다.\n먼저 연결을 중지해주세요.")
            return

        # 수정됨: self.engine 전달
        dialog = TunnelConfigDialog(self, tunnel_data=tunnel, tunnel_engine=self.engine)
        if dialog.exec():
            updated_data = dialog.get_data()
            updated_data = self._process_credentials(updated_data)
            for i, t in enumerate(self.tunnels):
                if t['id'] == updated_data['id']:
                    self.tunnels[i] = updated_data
                    break
            self.save_and_refresh()

    def delete_tunnel(self, tunnel):
        """연결 삭제"""
        if self.engine.is_running(tunnel['id']):
            QMessageBox.warning(self, "삭제 불가", "실행 중인 터널은 삭제할 수 없습니다.")
            return

        confirm = QMessageBox.question(self, "삭제 확인", f"'{tunnel['name']}' 연결 설정을 삭제하시겠습니까?",
                                       QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)

        if confirm == QMessageBox.StandardButton.Yes:
            # ID로 찾아서 삭제
            self.tunnels = [t for t in self.tunnels if t['id'] != tunnel['id']]
            self.save_and_refresh()

    def _process_credentials(self, tunnel_data: dict) -> dict:
        """비밀번호 암호화 처리"""
        result = tunnel_data.copy()

        # 평문 비밀번호가 있으면 암호화
        if '_db_password_plain' in result:
            plain_password = result.pop('_db_password_plain')
            if plain_password:
                result['db_password_encrypted'] = self.config_mgr.encryptor.encrypt(plain_password)

        # db_user가 없으면 관련 필드 모두 제거
        if not result.get('db_user'):
            result.pop('db_user', None)
            result.pop('db_password_encrypted', None)

        return result

    def save_and_refresh(self):
        """변경사항을 JSON 파일에 저장하고 테이블 새로고침 (기존 설정 보존)"""
        config = self.config_mgr.load_config()
        config['tunnels'] = self.tunnels
        self.config_mgr.save_config(config)
        self.refresh_table()
        self.statusBar().showMessage("설정이 저장되었습니다.", 2000)

    def open_settings_dialog(self):
        """설정 다이얼로그 열기"""
        dialog = SettingsDialog(self, config_manager=self.config_mgr)
        dialog.exec()

    def open_mysqlsh_export(self):
        """MySQL Shell Export 마법사 열기 (병렬 처리)"""
        wizard = MySQLShellWizard(
            parent=self,
            tunnel_engine=self.engine,
            config_manager=self.config_mgr
        )
        wizard.start_export()

    def open_mysqlsh_import(self):
        """MySQL Shell Import 마법사 열기"""
        wizard = MySQLShellWizard(
            parent=self,
            tunnel_engine=self.engine,
            config_manager=self.config_mgr
        )
        wizard.start_import()

    def open_migration_analyzer(self):
        """마이그레이션 분석기 열기"""
        MigrationWizard.start(
            parent=self,
            tunnel_engine=self.engine,
            config_manager=self.config_mgr
        )

    # --- 기존 터널링 로직 ---
    def start_tunnel(self, tunnel_config):
        self.statusBar().showMessage(f"연결 시도 중: {tunnel_config['name']}...")
        success, msg = self.engine.start_tunnel(tunnel_config)

        if success:
            self.statusBar().showMessage(f"연결 성공: {tunnel_config['name']}")
            self.tray_icon.showMessage("TunnelDB Manager", f"{tunnel_config['name']} 연결되었습니다.", QSystemTrayIcon.MessageIcon.Information, 2000)
        else:
            self.statusBar().showMessage(f"연결 실패: {msg}")
            QMessageBox.critical(self, "연결 오류", f"터널 연결에 실패했습니다.\n\n원인: {msg}")

        self.refresh_table()

    def stop_tunnel(self, tunnel_config):
        self.engine.stop_tunnel(tunnel_config['id'])
        self.statusBar().showMessage(f"연결 종료: {tunnel_config['name']}")
        self.refresh_table()

    def reload_config(self):
        self.config_data = self.config_mgr.load_config()
        self.tunnels = self.config_data.get('tunnels', [])
        self.refresh_table()
        QMessageBox.information(self, "알림", "설정 파일을 다시 불러왔습니다.")

    def closeEvent(self, event):
        """닫기 버튼 클릭 시"""
        close_action = self.config_mgr.get_app_setting('close_action', 'ask')

        if close_action == 'ask':
            # 다이얼로그 표시
            dialog = CloseConfirmDialog(self)
            if dialog.exec():
                action, remember = dialog.get_result()
                if remember:
                    self.config_mgr.set_app_setting('close_action', action)

                if action == 'minimize':
                    self.hide()
                    event.ignore()
                else:
                    self.close_app()
            else:
                event.ignore()  # 취소
        elif close_action == 'minimize':
            self.hide()
            event.ignore()
        else:  # 'exit'
            self.close_app()

    def close_app(self):
        """진짜 종료"""
        # 현재 활성화된 터널 ID 목록 저장 (다음 시작 시 자동 연결용)
        active_ids = list(self.engine.active_tunnels.keys())
        self.config_mgr.save_active_tunnels(active_ids)

        self.engine.stop_all()
        self.tray_icon.hide()
        # 모든 창 닫고 종료
        import sys
        sys.exit(0)

    def _check_update_on_startup(self):
        """앱 시작 시 업데이트 확인 (백그라운드)"""
        # 자동 업데이트 확인 설정 확인
        if not self.config_mgr.get_app_setting('auto_update_check', True):
            return

        # 백그라운드 스레드에서 확인
        self._update_checker_thread = StartupUpdateCheckerThread()
        self._update_checker_thread.update_available.connect(self._on_startup_update_available)
        self._update_checker_thread.start()

    def _auto_connect_tunnels(self):
        """앱 시작 시 이전에 활성화되어 있던 터널 자동 연결"""
        # 자동 연결 설정 확인
        if not self.config_mgr.get_app_setting('auto_reconnect', True):
            return

        last_active = self.config_mgr.get_last_active_tunnels()
        if not last_active:
            return

        print(f"🔄 이전 세션 터널 자동 연결 시도: {len(last_active)}개")

        connected = []
        skipped = []

        for tid in last_active:
            # 터널 설정 찾기
            tunnel = next((t for t in self.tunnels if t.get('id') == tid), None)
            if not tunnel:
                print(f"⚠️ 터널 설정을 찾을 수 없음: {tid}")
                continue

            # 연결 시도
            success, msg = self.engine.start_tunnel(tunnel, check_port=True)
            if success:
                connected.append(tunnel['name'])
                print(f"✅ 자동 연결 성공: {tunnel['name']}")
            else:
                skipped.append((tunnel['name'], msg))
                print(f"⚠️ 자동 연결 스킵: {tunnel['name']} - {msg}")

        # 테이블 갱신
        self.refresh_table()

        # 결과 알림
        if connected or skipped:
            msg_parts = []
            if connected:
                msg_parts.append(f"✅ 연결됨: {', '.join(connected)}")
            if skipped:
                skip_msgs = [f"{name} ({reason})" for name, reason in skipped]
                msg_parts.append(f"⚠️ 스킵: {', '.join(skip_msgs)}")

            self.statusBar().showMessage(" | ".join(msg_parts), 5000)

            # 트레이 알림 (연결된 터널이 있는 경우만)
            if connected:
                self.tray_icon.showMessage(
                    "자동 연결 완료",
                    f"{len(connected)}개 터널 연결됨" + (f", {len(skipped)}개 스킵" if skipped else ""),
                    QSystemTrayIcon.MessageIcon.Information,
                    3000
                )

    def _on_startup_update_available(self, latest_version: str, download_url: str):
        """시작 시 업데이트 발견 시 트레이 알림"""
        # 트레이 알림
        self.tray_icon.showMessage(
            "업데이트 사용 가능",
            f"새로운 버전 {latest_version}이 사용 가능합니다.\n설정에서 다운로드할 수 있습니다.",
            QSystemTrayIcon.MessageIcon.Information,
            5000  # 5초 동안 표시
        )

    # --- 컨텍스트 메뉴 ---
    def show_context_menu(self, position):
        """테이블 우클릭 컨텍스트 메뉴"""
        row = self.table.rowAt(position.y())
        # 범위 밖이면 무시
        if row < 0 or row >= len(self.tunnels):
            return

        tunnel = self.tunnels[row]
        menu = QMenu(self)

        # Shell Export/Import
        menu.addAction("🚀 Shell Export", lambda: self._context_shell_export(tunnel))
        menu.addAction("📥 Shell Import", lambda: self._context_shell_import(tunnel))

        menu.addSeparator()

        # SQL 에디터 및 실행
        menu.addAction("📝 SQL 에디터 열기...", lambda: self.open_sql_editor(tunnel))
        menu.addAction("📄 SQL 파일 실행...", lambda: self.run_sql_file(tunnel))

        menu.exec(self.table.mapToGlobal(position))

    def open_sql_editor(self, tunnel):
        """SQL 에디터 다이얼로그 열기"""
        # 자격 증명 확인
        user, _ = self.config_mgr.get_tunnel_credentials(tunnel['id'])
        if not user:
            QMessageBox.warning(
                self, "경고",
                "DB 자격 증명이 저장되어 있지 않습니다.\n터널 설정에서 DB 사용자/비밀번호를 저장해주세요."
            )
            return

        # 터널 비활성화시 자동 활성화 (직접 연결 모드 제외)
        is_direct = tunnel.get('connection_mode') == 'direct'
        if not is_direct and not self.engine.is_running(tunnel['id']):
            reply = QMessageBox.question(
                self, "터널 연결",
                f"'{tunnel['name']}' 터널이 연결되어 있지 않습니다.\n터널을 시작하시겠습니까?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if reply == QMessageBox.StandardButton.Yes:
                success, msg = self.engine.start_tunnel(tunnel)
                if not success:
                    QMessageBox.critical(self, "오류", f"터널 시작 실패:\n{msg}")
                    return
                self.refresh_table()
            else:
                return

        dialog = SQLEditorDialog(self, tunnel, self.config_mgr, self.engine)
        dialog.exec()

    def run_sql_file(self, tunnel):
        """SQL 파일 실행 다이얼로그"""
        # 자격 증명 확인
        user, _ = self.config_mgr.get_tunnel_credentials(tunnel['id'])
        if not user:
            QMessageBox.warning(
                self, "경고",
                "DB 자격 증명이 저장되어 있지 않습니다.\n터널 설정에서 DB 사용자/비밀번호를 저장해주세요."
            )
            return

        dialog = SQLExecutionDialog(self, tunnel, self.config_mgr, self.engine)
        dialog.exec()

    def _context_shell_export(self, tunnel):
        """특정 터널용 Shell Export - 인증정보 자동 사용"""
        # 자격 증명 확인
        user, _ = self.config_mgr.get_tunnel_credentials(tunnel['id'])
        if not user:
            QMessageBox.warning(
                self, "경고",
                "DB 자격 증명이 저장되어 있지 않습니다.\n터널 설정에서 DB 사용자/비밀번호를 저장해주세요."
            )
            return

        # 터널 비활성화시 자동 활성화 (직접 연결 모드 제외)
        is_direct = tunnel.get('connection_mode') == 'direct'
        if not is_direct and not self.engine.is_running(tunnel['id']):
            success, msg = self.engine.start_tunnel(tunnel)
            if not success:
                QMessageBox.critical(self, "오류", f"터널 시작 실패:\n{msg}")
                return
            self.refresh_table()

        wizard = MySQLShellWizard(
            parent=self,
            tunnel_engine=self.engine,
            config_manager=self.config_mgr,
            preselected_tunnel=tunnel
        )
        wizard.start_export()

    def _context_shell_import(self, tunnel):
        """특정 터널용 Shell Import - 인증정보 자동 사용"""
        # 자격 증명 확인
        user, _ = self.config_mgr.get_tunnel_credentials(tunnel['id'])
        if not user:
            QMessageBox.warning(
                self, "경고",
                "DB 자격 증명이 저장되어 있지 않습니다.\n터널 설정에서 DB 사용자/비밀번호를 저장해주세요."
            )
            return

        # 터널 비활성화시 자동 활성화 (직접 연결 모드 제외)
        is_direct = tunnel.get('connection_mode') == 'direct'
        if not is_direct and not self.engine.is_running(tunnel['id']):
            success, msg = self.engine.start_tunnel(tunnel)
            if not success:
                QMessageBox.critical(self, "오류", f"터널 시작 실패:\n{msg}")
                return
            self.refresh_table()

        wizard = MySQLShellWizard(
            parent=self,
            tunnel_engine=self.engine,
            config_manager=self.config_mgr,
            preselected_tunnel=tunnel
        )
        wizard.start_import()
