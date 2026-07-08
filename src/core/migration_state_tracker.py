"""
MySQL 8.0 → 8.4 마이그레이션 단계 상수

마이그레이션 진행 상태의 저장/재시작/롤백은 Rust DB Core로 이관되었다.
이 모듈에는 UI가 표시할 단계를 나타내는 MigrationPhase 상수만 남아 있다.
"""


class MigrationPhase:
    """마이그레이션 단계"""
    PREFLIGHT = "preflight"       # 사전 검사
    ANALYSIS = "analysis"         # 분석
    RECOMMENDATION = "recommendation"  # 권장 옵션 선택
    EXECUTION = "execution"       # 실행
    VALIDATION = "validation"     # 검증
    COMPLETED = "completed"       # 완료
