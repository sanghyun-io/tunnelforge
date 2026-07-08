"""
MySQL 8.0 → 8.4 마이그레이션 Pre-flight Check 결과 데이터 셰이프

실제 사전 검증(권한/디스크/활성 연결/백업/버전 검사)은 Rust DB Core로
이관되었다. 이 모듈에는 Rust가 보낸 preflight 이벤트를 UI에 표현하기 위한
데이터클래스만 남아 있다.
"""
from dataclasses import dataclass, field
from typing import List, Optional
from enum import Enum


class CheckSeverity(Enum):
    """검사 결과 심각도"""
    ERROR = "error"      # 마이그레이션 불가
    WARNING = "warning"  # 주의 필요
    INFO = "info"        # 정보성


@dataclass
class CheckResult:
    """개별 검사 결과"""
    name: str
    passed: bool
    severity: CheckSeverity
    message: str
    details: Optional[str] = None

    @property
    def severity_str(self) -> str:
        return self.severity.value


@dataclass
class PreflightResult:
    """Pre-flight 전체 결과"""
    passed: bool
    checks: List[CheckResult] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    @property
    def error_count(self) -> int:
        return len([c for c in self.checks if not c.passed and c.severity == CheckSeverity.ERROR])

    @property
    def warning_count(self) -> int:
        return len([c for c in self.checks if c.severity == CheckSeverity.WARNING])

    def get_summary(self) -> str:
        """결과 요약 문자열 반환"""
        if self.passed:
            return f"✅ Pre-flight 통과 ({len(self.checks)}개 검사, 경고 {self.warning_count}개)"
        else:
            return f"❌ Pre-flight 실패 (오류 {self.error_count}개, 경고 {self.warning_count}개)"
