"""Small platform integration helpers isolated from UI/business logic."""
import os
import plistlib
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Optional, Tuple
from xml.sax.saxutils import escape


def _platform_name(platform_name: Optional[str] = None) -> str:
    if platform_name:
        return platform_name
    if sys.platform == "win32":
        return "Windows"
    if sys.platform == "darwin":
        return "Darwin"
    return sys.platform


def is_windows(platform_name: Optional[str] = None) -> bool:
    return _platform_name(platform_name) == "Windows"


def is_macos(platform_name: Optional[str] = None) -> bool:
    return _platform_name(platform_name) == "Darwin"


def detached_process_kwargs(platform_name: Optional[str] = None) -> dict:
    """Return subprocess kwargs for detached child processes on Windows."""
    if not is_windows(platform_name):
        return {}

    flags = 0
    for name in ("DETACHED_PROCESS", "CREATE_NEW_PROCESS_GROUP"):
        flags |= int(getattr(subprocess, name, 0))
    return {"creationflags": flags} if flags else {}


def no_window_creation_flags() -> int:
    """Return CREATE_NO_WINDOW where available, otherwise no flags."""
    if os.name != "nt":
        return 0
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))


def set_app_user_model_id(app_id: str) -> None:
    """Set the Windows taskbar AppUserModelID; no-op on other platforms."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
    except Exception:
        return


def restore_window_to_front(window_id: int, platform_name: Optional[str] = None) -> bool:
    """Use native Windows APIs to restore a window; no-op elsewhere."""
    if not is_windows(platform_name):
        return False
    try:
        import ctypes

        ctypes.windll.user32.ShowWindow(int(window_id), 9)
        ctypes.windll.user32.SetForegroundWindow(int(window_id))
        return True
    except Exception:
        return False


def update_package_launch_strategy(platform_name: Optional[str] = None) -> str:
    """Return how an update package should be launched for the platform."""
    return "execute" if is_windows(platform_name) else "open"


def show_crash_recovery_message(error_message: str, app_dir: str) -> None:
    """Show a platform-appropriate startup crash message."""
    if sys.platform == "win32":
        _show_windows_crash_recovery_message(error_message, app_dir)
        return

    message = (
        "TunnelForge startup error\n\n"
        f"{error_message}\n\n"
        "Download the latest release from:\n"
        "https://github.com/sanghyun-io/tunnelforge/releases"
    )
    try:
        sys.stderr.write(message + "\n")
    except Exception:
        pass


def _show_windows_crash_recovery_message(error_message: str, app_dir: str) -> None:
    import ctypes

    mb_yesno = 0x04
    mb_iconerror = 0x10
    id_yes = 6
    updater_path = os.path.join(app_dir, "TunnelForge-WebSetup.exe")
    updater_exists = os.path.exists(updater_path)

    if updater_exists:
        message = (
            f"프로그램 실행 중 오류가 발생했습니다.\n\n"
            f"오류 내용:\n{error_message}\n\n"
            f"복구/업데이트 프로그램을 실행하여 최신 버전으로 재설치하시겠습니까?"
        )
        result = ctypes.windll.user32.MessageBoxW(
            None,
            message,
            "TunnelForge - 오류",
            mb_yesno | mb_iconerror,
        )
        if result == id_yes:
            try:
                subprocess.Popen([updater_path], **detached_process_kwargs("Windows"))
            except Exception as exc:
                ctypes.windll.user32.MessageBoxW(
                    None,
                    f"복구 프로그램 실행 실패:\n{str(exc)}\n\n"
                    f"수동으로 실행해 주세요:\n{updater_path}",
                    "TunnelForge - 오류",
                    mb_iconerror,
                )
        return

    message = (
        f"프로그램 실행 중 오류가 발생했습니다.\n\n"
        f"오류 내용:\n{error_message}\n\n"
        f"GitHub에서 최신 버전을 다운로드해 주세요:\n"
        f"https://github.com/sanghyun-io/tunnelforge/releases"
    )
    ctypes.windll.user32.MessageBoxW(None, message, "TunnelForge - 오류", mb_iconerror)


class WindowsStartupRegistrar:
    """Windows 시작 프로그램(레지스트리 Run 키) 등록 전략."""

    def is_registered(self) -> bool:
        try:
            import winreg

            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Run",
                0,
                winreg.KEY_READ,
            )
            try:
                winreg.QueryValueEx(key, "TunnelForge")
                return True
            finally:
                winreg.CloseKey(key)
        except Exception:
            return False

    def set_registered(self, enable: bool) -> Tuple[bool, str]:
        try:
            import winreg

            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Run",
                0,
                winreg.KEY_SET_VALUE,
            )
            try:
                if enable:
                    app_path = self._startup_command()
                    winreg.SetValueEx(key, "TunnelForge", 0, winreg.REG_SZ, app_path)
                else:
                    try:
                        winreg.DeleteValue(key, "TunnelForge")
                    except FileNotFoundError:
                        pass
            finally:
                winreg.CloseKey(key)
            return True, ""
        except Exception as exc:
            return False, str(exc)

    def _startup_command(self) -> str:
        if getattr(sys, "frozen", False):
            return f'"{sys.executable}" --minimized'

        python_dir = os.path.dirname(sys.executable)
        pythonw = os.path.join(python_dir, "pythonw.exe")
        if not os.path.exists(pythonw):
            pythonw = sys.executable
        main_script = os.path.abspath("main.py")
        return f'"{pythonw}" "{main_script}" --minimized'


class MacOSStartupRegistrar:
    """macOS LaunchAgent(plist) 등록 전략."""

    def __init__(self, home: Optional[Path] = None, executable: Optional[str] = None):
        self.home = home
        self.executable = executable

    def is_registered(self) -> bool:
        return self._launch_agent_matches_current_app()

    def set_registered(self, enable: bool) -> Tuple[bool, str]:
        path = self._launch_agent_path()
        try:
            if enable:
                path.parent.mkdir(parents=True, exist_ok=True)
                self._launch_agent_log_dir().mkdir(parents=True, exist_ok=True)
                path.write_text(self._launch_agent_plist(), encoding="utf-8")
            else:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
            return True, ""
        except Exception as exc:
            return False, str(exc)

    def _home_path(self) -> Path:
        return Path(self.home) if self.home is not None else Path.home()

    def _launch_agent_path(self) -> Path:
        return self._home_path() / "Library" / "LaunchAgents" / "io.sanghyun.tunnelforge.plist"

    def _launch_agent_log_dir(self) -> Path:
        return self._home_path() / "Library" / "Logs" / "TunnelForge"

    def _launch_agent_matches_current_app(self) -> bool:
        path = self._launch_agent_path()
        if not path.exists():
            return False

        try:
            launch_agent = plistlib.loads(path.read_bytes())
        except Exception:
            return False

        return (
            launch_agent.get("ProgramArguments") == list(self._startup_arguments())
            and launch_agent.get("WorkingDirectory") == self._working_directory()
        )

    def _startup_arguments(self) -> Tuple[str, ...]:
        executable = self.executable or sys.executable
        if getattr(sys, "frozen", False):
            return (executable, "--minimized")
        main_script = os.path.abspath("main.py")
        return (executable, main_script, "--minimized")

    def _working_directory(self) -> str:
        executable = str(self.executable or sys.executable)
        if getattr(sys, "frozen", False):
            return str(PurePosixPath(executable).parent)
        return str(Path(os.path.abspath("main.py")).parent)

    def _launch_agent_plist(self) -> str:
        args = "\n".join(
            f"        <string>{escape(str(arg))}</string>"
            for arg in self._startup_arguments()
        )
        log_dir = self._launch_agent_log_dir()
        stdout_path = escape(str(log_dir / "launchagent.out.log"))
        stderr_path = escape(str(log_dir / "launchagent.err.log"))
        working_directory = escape(self._working_directory())
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>io.sanghyun.tunnelforge</string>
    <key>ProgramArguments</key>
    <array>
{args}
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>WorkingDirectory</key>
    <string>{working_directory}</string>
    <key>StandardOutPath</key>
    <string>{stdout_path}</string>
    <key>StandardErrorPath</key>
    <string>{stderr_path}</string>
</dict>
</plist>
"""


@dataclass
class StartupRegistrar:
    """OS-specific startup registration facade.

    실제 등록 로직은 플랫폼별 전략(WindowsStartupRegistrar/MacOSStartupRegistrar)에
    위임하고, 이 클래스는 platform_name으로 전략을 고르는 얇은 facade 역할만 한다.
    """

    platform_name: Optional[str] = None
    home: Optional[Path] = None
    executable: Optional[str] = None

    @property
    def is_supported(self) -> bool:
        return is_windows(self.platform_name) or is_macos(self.platform_name)

    def _strategy(self):
        if is_macos(self.platform_name):
            return MacOSStartupRegistrar(home=self.home, executable=self.executable)
        return WindowsStartupRegistrar()

    def is_registered(self) -> bool:
        if not self.is_supported:
            return False
        return self._strategy().is_registered()

    def set_registered(self, enable: bool) -> Tuple[bool, str]:
        if not self.is_supported:
            return False, "자동 시작은 아직 이 플랫폼에서 지원되지 않습니다."
        return self._strategy().set_registered(enable)
