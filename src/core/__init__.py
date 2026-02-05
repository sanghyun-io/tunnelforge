from .config_manager import ConfigManager
from .tunnel_engine import TunnelEngine
from .db_connector import MySQLConnector, test_mysql_connection
from .github_issue_reporter import GitHubIssueReporter, get_reporter_from_config
from .github_app_auth import GitHubAppAuth, get_github_app_auth, is_github_app_configured
from .migration_analyzer import MigrationAnalyzer, AnalysisResult, OrphanRecord, CleanupAction, ActionType
from .sql_history import SQLHistory
from .sql_validator import (
    SQLValidator, SQLAutoCompleter, SchemaMetadataProvider,
    SchemaMetadata, ValidationIssue, IssueSeverity
)
from .production_guard import ProductionGuard, Environment, SchemaConfirmDialog
# One-Click 마이그레이션 모듈
from .migration_preflight import PreflightChecker, PreflightResult, CheckResult, CheckSeverity
from .migration_auto_recommend import AutoRecommendationEngine, RecommendationSummary
from .migration_state_tracker import MigrationStateTracker, MigrationState, MigrationPhase, get_state_tracker
from .migration_validator import PostMigrationValidator, ValidationResult, MigrationReport

__all__ = [
    'ConfigManager', 'TunnelEngine', 'MySQLConnector', 'test_mysql_connection',
    'GitHubIssueReporter', 'get_reporter_from_config',
    'GitHubAppAuth', 'get_github_app_auth', 'is_github_app_configured',
    'MigrationAnalyzer', 'AnalysisResult', 'OrphanRecord', 'CleanupAction', 'ActionType',
    'SQLHistory',
    'SQLValidator', 'SQLAutoCompleter', 'SchemaMetadataProvider',
    'SchemaMetadata', 'ValidationIssue', 'IssueSeverity',
    'ProductionGuard', 'Environment', 'SchemaConfirmDialog',
    # One-Click 마이그레이션
    'PreflightChecker', 'PreflightResult', 'CheckResult', 'CheckSeverity',
    'AutoRecommendationEngine', 'RecommendationSummary',
    'MigrationStateTracker', 'MigrationState', 'MigrationPhase', 'get_state_tracker',
    'PostMigrationValidator', 'ValidationResult', 'MigrationReport'
]
