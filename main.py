# -*- coding: utf-8 -*-
import sys
import os
import traceback
import json
import subprocess
from contextlib import redirect_stdout

# Windows 콘솔 UTF-8 출력 지원 (이모지 출력을 위해)
# GUI 모드(pythonw.exe 또는 PyInstaller --noconsole)에서는 stdout/stderr가 None일 수 있음
if sys.platform == 'win32':
    if sys.stdout is not None and hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace', line_buffering=True)
    if sys.stderr is not None and hasattr(sys.stderr, 'reconfigure'):
        sys.stderr.reconfigure(encoding='utf-8', errors='replace', line_buffering=True)


def _stream_is_usable(stream) -> bool:
    if stream is None:
        return False
    try:
        stream.write("")
        stream.flush()
        return True
    except (AttributeError, OSError, ValueError):
        return False


def write_cli_json(result: dict) -> None:
    """Write CLI JSON without crashing noconsole PyInstaller builds."""
    payload = json.dumps(result, ensure_ascii=False, sort_keys=True)
    for stream in (sys.stdout, getattr(sys, "__stdout__", None)):
        if not _stream_is_usable(stream):
            continue
        try:
            stream.write(payload + "\n")
            stream.flush()
            return
        except (AttributeError, OSError, ValueError):
            continue

    output_path = os.environ.get("TUNNELFORGE_CLI_OUTPUT")
    if output_path:
        try:
            with open(output_path, "w", encoding="utf-8") as output_file:
                output_file.write(payload + "\n")
        except OSError:
            pass


def get_app_dir() -> str:
    """애플리케이션 설치 경로 반환"""
    if getattr(sys, 'frozen', False):
        # PyInstaller 빌드된 경우
        return os.path.dirname(sys.executable)
    else:
        # 개발 환경
        return os.path.dirname(os.path.abspath(__file__))


def show_error_and_offer_recovery(error_message: str):
    """오류 메시지 표시 및 복구 프로그램 실행 제안."""
    try:
        from src.core.platform_integration import show_crash_recovery_message

        show_crash_recovery_message(error_message, get_app_dir())
    except Exception:
        if sys.platform == 'win32':
            try:
                import ctypes

                ctypes.windll.user32.MessageBoxW(
                    None,
                    f"프로그램 실행 중 오류가 발생했습니다.\n\n오류 내용:\n{error_message}",
                    "TunnelForge - 오류",
                    0x10,
                )
                return
            except Exception:
                pass
        try:
            sys.stderr.write(f"TunnelForge startup error\n\n{error_message}\n")
        except Exception:
            pass


def app_icon_path():
    from src.core.resources import app_icon_path as resolve_app_icon_path

    return resolve_app_icon_path()


def db_core_executable() -> str:
    from src.core.cross_engine_migration import db_core_executable as resolve_db_core_executable

    return resolve_db_core_executable()


def _lazy_class(module_path: str, name: str):
    module = __import__(module_path, fromlist=[name])
    return getattr(module, name)


def _load_qapplication_class():
    return _lazy_class("PyQt6.QtWidgets", "QApplication")


def _load_qicon_class():
    return _lazy_class("PyQt6.QtGui", "QIcon")


def _load_config_manager_class():
    return _lazy_class("src.core", "ConfigManager")


def _load_tunnel_engine_class():
    return _lazy_class("src.core", "TunnelEngine")


def _load_tunnel_manager_ui_class():
    return _lazy_class("src.ui.main_window", "TunnelManagerUI")


def should_run_self_check(argv=None) -> bool:
    return "--self-check" in (argv if argv is not None else sys.argv)


def should_run_ui_smoke_check(argv=None) -> bool:
    return "--ui-smoke-check" in (argv if argv is not None else sys.argv)


def run_self_check() -> dict:
    """Verify packaged-runtime essentials without starting the GUI event loop."""
    icon = app_icon_path()
    core = db_core_executable()
    result = {
        "success": False,
        "icon_path": str(icon),
        "icon_exists": icon.exists(),
        "core_path": str(core),
        "core_exists": os.path.exists(core),
        "core_hello": None,
        "core_error": None,
    }

    if not result["icon_exists"] or not result["core_exists"]:
        return result

    request = {
        "command": "service.hello",
        "request_id": "self-check",
        "payload": {},
    }
    try:
        completed = subprocess.run(
            [core],
            input=json.dumps(request, ensure_ascii=False) + "\n",
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=10,
        )
        completed.check_returncode()
        result["core_hello"] = json.loads(completed.stdout.strip().splitlines()[-1])
    except Exception as exc:
        result["core_error"] = f"{type(exc).__name__}: {exc}"
        return result

    result["success"] = (
        result["core_hello"].get("event") == "result"
        and result["core_hello"].get("request_id") == "self-check"
        and result["core_hello"].get("service") == "tunnelforge-core"
        and result["core_hello"].get("success") is True
    )
    return result


def run_self_check_cli() -> int:
    result = run_self_check()
    write_cli_json(result)
    return 0 if result["success"] else 1


def run_ui_smoke_check() -> dict:
    """Construct the main PyQt window without background work or event loop."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    self_check = run_self_check()

    app_cls = _load_qapplication_class()
    icon_cls = _load_qicon_class()
    config_cls = _load_config_manager_class()
    engine_cls = _load_tunnel_engine_class()
    window_cls = _load_tunnel_manager_ui_class()
    config_mgr = config_cls()
    from src.core.i18n import configure_language, install_qt_i18n

    configure_language(config_mgr, ["TunnelForge", "--ui-smoke-check"])
    install_qt_i18n()

    app = app_cls.instance() or app_cls(["TunnelForge", "--ui-smoke-check"])
    app.setWindowIcon(icon_cls(str(app_icon_path())))

    window = window_cls(config_mgr, engine_cls(), start_background=False)
    result = {
        "success": self_check["success"] is True and window.windowTitle() == "TunnelForge",
        "window_title": window.windowTitle(),
        "self_check": self_check,
    }
    window.dispose_for_smoke_check()
    return result


def run_ui_smoke_check_cli() -> int:
    redirect_target = sys.stderr
    close_redirect_target = False
    if not _stream_is_usable(redirect_target):
        redirect_target = open(os.devnull, "w", encoding="utf-8")
        close_redirect_target = True
    try:
        with redirect_stdout(redirect_target):
            result = run_ui_smoke_check()
    finally:
        if close_redirect_target:
            redirect_target.close()
    write_cli_json(result)
    return 0 if result["success"] else 1


def main():
    if should_run_self_check():
        return run_self_check_cli()
    if should_run_ui_smoke_check():
        return run_ui_smoke_check_cli()

    from src.core.logger import get_logger
    from src.core.platform_integration import set_app_user_model_id
    from src.core.single_instance import SingleInstanceGuard

    # 루트 로거 초기화
    logger = get_logger('main')

    # Windows 작업표시줄 아이콘을 위한 AppUserModelID 설정
    set_app_user_model_id('tunnelforge.1.0')

    app_cls = _load_qapplication_class()
    icon_cls = _load_qicon_class()
    config_cls = _load_config_manager_class()
    engine_cls = _load_tunnel_engine_class()
    window_cls = _load_tunnel_manager_ui_class()

    app = app_cls(sys.argv)
    app.setWindowIcon(icon_cls(str(app_icon_path())))

    single_instance_guard = SingleInstanceGuard(parent=app)
    if single_instance_guard.is_secondary:
        if SingleInstanceGuard.notify_existing_instance():
            logger.info("이미 실행 중인 TunnelForge 인스턴스에 창 활성화를 요청했습니다.")
        else:
            logger.warning("TunnelForge가 이미 실행 중이지만 창 활성화 요청을 보내지 못했습니다.")
        return 0

    pending_activation_requests = []
    single_instance_guard.activation_requested.connect(
        lambda: pending_activation_requests.append(True)
    )
    app.aboutToQuit.connect(single_instance_guard.close)

    # 애플리케이션가 닫혀도 마지막 창이 닫힐 때까지 종료되지 않도록 설정 (트레이 아이콘 때문)
    app.setQuitOnLastWindowClosed(False)

    # 1. 매니저 초기화
    config_mgr = config_cls()
    from src.core.i18n import configure_language, install_qt_i18n

    configure_language(config_mgr, sys.argv)
    install_qt_i18n()
    tunnel_engine = engine_cls()

    # 2. 설정 파일 경로 안내 (첫 실행 사용자를 위해)
    config_path = config_mgr.get_config_path()
    logger.info(f"설정 파일 위치: {config_path}")

    # 3. UI 실행
    start_minimized = '--minimized' in sys.argv
    window = window_cls(config_mgr, tunnel_engine)
    single_instance_guard.activation_requested.connect(window.bring_to_front)
    if not start_minimized:
        window.show()
    else:
        logger.info("--minimized 모드: 시스템 트레이에서 시작")
    if pending_activation_requests:
        window.bring_to_front()

    # 4. 앱 루프 시작
    sys.exit(app.exec())


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        # 전역 예외 핸들러: 시작 시 오류 발생하면 복구 프로그램 안내
        error_msg = f"{type(e).__name__}: {str(e)}"

        # 상세 에러 로그 (디버깅용)
        try:
            error_details = traceback.format_exc()
            # 로그 파일에 기록 시도
            app_dir = get_app_dir()
            log_path = os.path.join(app_dir, "crash.log")
            with open(log_path, 'w', encoding='utf-8') as f:
                f.write(f"TunnelForge Crash Log\n")
                f.write(f"=" * 50 + "\n")
                f.write(error_details)
        except:
            pass  # 로그 기록 실패해도 계속 진행

        show_error_and_offer_recovery(error_msg)
        sys.exit(1)
