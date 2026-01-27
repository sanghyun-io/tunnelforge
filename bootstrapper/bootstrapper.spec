# -*- mode: python ; coding: utf-8 -*-
"""TunnelDB Manager 부트스트래퍼 PyInstaller 설정

경량 온라인 설치 프로그램을 위한 빌드 설정입니다.
불필요한 모듈을 제외하여 파일 크기를 최소화합니다.
"""

import os
import sys

# 프로젝트 루트 경로
project_root = os.path.dirname(os.path.dirname(os.path.abspath(SPEC)))

block_cipher = None

a = Analysis(
    [os.path.join(project_root, 'bootstrapper', 'bootstrapper.py')],
    pathex=[project_root],
    binaries=[],
    datas=[
        # 아이콘 파일 포함
        (os.path.join(project_root, 'assets', 'icon.ico'), 'assets'),
    ],
    hiddenimports=[
        'requests',
        'urllib3',
        'certifi',
        'charset_normalizer',
        'idna',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # PyQt 관련 (사용하지 않음)
        'PyQt5', 'PyQt6', 'PySide2', 'PySide6',
        # 데이터 처리 (사용하지 않음)
        'numpy', 'pandas', 'scipy', 'matplotlib',
        # 데이터베이스 (사용하지 않음)
        'pymysql', 'sqlite3', 'sqlalchemy',
        # SSH/암호화 (사용하지 않음)
        'paramiko', 'cryptography', 'nacl',
        # 테스트/개발 (사용하지 않음)
        'pytest', 'unittest', 'setuptools', 'pip',
        # 기타 불필요한 모듈
        'PIL', 'cv2', 'IPython', 'jupyter',
        'xml', 'http.server',
        'asyncio', 'concurrent', 'multiprocessing',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='TunnelDBManager-WebSetup',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,  # GUI 모드
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=os.path.join(project_root, 'assets', 'icon.ico'),
    version_file=None,
)
