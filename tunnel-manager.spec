# -*- mode: python ; coding: utf-8 -*-

"""
TunnelForge - PyInstaller Spec 파일

이 파일은 PyInstaller가 실행 파일을 생성할 때 사용하는 설정입니다.
빌드 방법: pyinstaller tunnel-manager.spec
"""

import os
from PyInstaller.utils.hooks import collect_submodules

# 프로젝트 루트 디렉터리
project_root = os.path.abspath(SPECPATH)

# 숨겨진 import 목록 (PyInstaller가 자동으로 감지하지 못하는 모듈)
hiddenimports = [
    # PyQt6 관련
    'PyQt6.QtCore',
    'PyQt6.QtGui',
    'PyQt6.QtWidgets',
    'PyQt6.sip',

    # SSH 및 암호화 관련
    'sshtunnel',
    'paramiko',
    'paramiko.dsskey',
    'paramiko.rsakey',
    'paramiko.ecdsakey',
    'paramiko.ed25519key',
    'paramiko.transport',
    'paramiko.client',
    'paramiko.pkey',
    'cryptography',
    'cryptography.hazmat.backends',
    'cryptography.hazmat.primitives',
    'cryptography.hazmat.primitives.asymmetric',
    'cryptography.hazmat.primitives.asymmetric.ed25519',
    'cryptography.hazmat.primitives.asymmetric.rsa',
    'cryptography.hazmat.primitives.asymmetric.ec',
    'cryptography.hazmat.primitives.asymmetric.dsa',

    # 데이터베이스 관련
    'pymysql',
    'pymysql.cursors',
    'pymysql.converters',

    # 기타 의존성
    'requests',
    'PyJWT',
    'dotenv',
]

# 제외할 모듈 (불필요한 대용량 라이브러리 제외로 크기 최적화)
excludes = [
    'matplotlib',
    'numpy',
    'pandas',
    'scipy',
    'IPython',
    'jupyter',
]

# 포함할 데이터 파일 (형식: (소스 경로, 대상 경로))
datas = [
    (os.path.join(project_root, 'assets'), 'assets'),  # 아이콘 및 리소스 파일
]

# Analysis: 실행 파일에 포함될 내용 분석
a = Analysis(
    [os.path.join(project_root, 'main.py')],  # 메인 스크립트
    pathex=[project_root],  # 추가 모듈 검색 경로
    binaries=[],  # 추가 바이너리 파일 (필요시)
    datas=datas,  # 리소스 파일
    hiddenimports=hiddenimports,  # 숨겨진 import
    hookspath=[],  # 커스텀 PyInstaller 훅 경로
    hooksconfig={},  # 훅 설정
    runtime_hooks=[],  # 런타임 훅
    excludes=excludes,  # 제외할 모듈
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=None,  # 암호화 (필요시 설정)
    noarchive=False,  # False: PYZ 아카이브 사용
)

# PYZ: Python 코드 아카이브
pyz = PYZ(
    a.pure,
    a.zipped_data,
    cipher=None,
)

# EXE: 실행 파일 생성
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='TunnelForge',  # 실행 파일 이름
    debug=False,  # True: 디버그 정보 포함
    bootloader_ignore_signals=False,
    strip=False,  # True: 바이너리에서 심볼 제거 (리눅스/맥)
    upx=True,  # True: UPX로 압축 (UPX 설치 필요)
    upx_exclude=[],  # UPX 압축 제외 파일
    runtime_tmpdir=None,  # 런타임 임시 디렉터리
    console=False,  # False: 콘솔 창 숨김 (GUI 전용), True: 콘솔 표시 (디버깅용)
    disable_windowed_traceback=False,
    target_arch=None,  # None: 현재 아키텍처, 'x86_64' 또는 'arm64' 지정 가능
    codesign_identity=None,  # 코드 서명 (맥OS)
    entitlements_file=None,  # 권한 파일 (맥OS)
    icon=os.path.join(project_root, 'assets', 'icon.ico'),  # 실행 파일 아이콘
)

# COLLECT: 디렉터리 모드로 빌드 시 사용
# 단일 파일 모드(onefile)를 원하면 이 섹션을 주석 처리하세요
# coll = COLLECT(
#     exe,
#     a.binaries,
#     a.zipfiles,
#     a.datas,
#     strip=False,
#     upx=True,
#     upx_exclude=[],
#     name='TunnelForge',
# )
