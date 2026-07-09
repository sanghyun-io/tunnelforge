"""
마이그레이션 수동 SQL 가이드 다이얼로그
"""
import html
import re
from PyQt6.QtWidgets import (
    QDialog, QHBoxLayout, QHeaderView, QLabel, QPushButton, QSplitter,
    QTableWidget, QTableWidgetItem, QTextEdit, QVBoxLayout, QWidget
)
from PyQt6.QtCore import Qt

from src.core.migration_analyzer import IssueType
from src.core.migration_constants import ISSUE_TYPE_DISPLAY_NAMES


class ManualGuideDialog(QDialog):
    """수동 처리 가이드 다이얼로그

    자동 수정이 불가능한 이슈에 대한 수동 처리 방법을 안내합니다.
    """

    # 이슈 유형별 가이드
    GUIDES = {
        IssueType.AUTH_PLUGIN_ISSUE: {
            "title": "인증 플러그인 이슈",
            "description": "MySQL 8.4에서 mysql_native_password가 기본 비활성화됩니다.",
            "solution": """**해결 방법:**

1. **권장: caching_sha2_password로 변경**
   ```sql
   ALTER USER 'username'@'host' IDENTIFIED WITH caching_sha2_password BY '새_비밀번호';
   ```

2. **임시 해결: mysql_native_password 유지 (비권장)**
   my.cnf에 추가:
   ```
   [mysqld]
   mysql_native_password=ON
   ```

**주의:** 비밀번호를 모르면 사용자에게 새 비밀번호를 설정하도록 안내하세요.""",
        },
        IssueType.RESERVED_KEYWORD: {
            "title": "예약어 충돌",
            "description": "MySQL 8.4에서 새로운 예약어가 추가되어 기존 식별자와 충돌합니다.",
            "solution": """**해결 방법:**

1. **백틱(`)으로 감싸기**
   ```sql
   SELECT `groups` FROM users;  -- groups가 예약어인 경우
   ```

2. **이름 변경 (권장)**
   ```sql
   ALTER TABLE old_name RENAME TO new_name;
   ALTER TABLE tbl RENAME COLUMN old_col TO new_col;
   ```

**주의:** 애플리케이션 코드에서도 해당 식별자를 사용하는 모든 곳을 수정해야 합니다.""",
        },
        IssueType.FK_NAME_LENGTH: {
            "title": "FK 이름 길이 초과",
            "description": "FK 제약조건 이름이 64자를 초과합니다.",
            "solution": """**해결 방법:**

1. **FK 삭제 후 짧은 이름으로 재생성**
   ```sql
   -- 기존 FK 삭제
   ALTER TABLE child_table DROP FOREIGN KEY too_long_fk_name_xxx;

   -- 짧은 이름으로 재생성
   ALTER TABLE child_table
   ADD CONSTRAINT fk_short_name
   FOREIGN KEY (col) REFERENCES parent_table(col);
   ```

**팁:** FK 이름 규칙 예시: `fk_자식테이블_부모테이블` (64자 이내)""",
        },
        IssueType.PARTITION_ISSUE: {
            "title": "파티션 이슈",
            "description": "파티션 테이블에 호환성 문제가 있습니다.",
            "solution": """**해결 방법:**

1. **파티션 재구성**
   ```sql
   ALTER TABLE tbl REORGANIZE PARTITION ...;
   ```

2. **파티션 제거 후 재생성**
   ```sql
   ALTER TABLE tbl REMOVE PARTITIONING;
   -- 새 파티션 스키마로 재생성
   ```

**주의:** 데이터 양이 많은 경우 시간이 오래 걸릴 수 있습니다. 유지보수 시간에 수행하세요.""",
        },
        IssueType.INDEX_ISSUE: {
            "title": "인덱스 이슈",
            "description": "인덱스에 호환성 문제가 있습니다.",
            "solution": """**해결 방법:**

1. **인덱스 재생성**
   ```sql
   DROP INDEX idx_name ON table_name;
   CREATE INDEX idx_name ON table_name (columns);
   ```

2. **ALGORITHM=INPLACE 사용 (온라인 DDL)**
   ```sql
   ALTER TABLE tbl DROP INDEX idx, ADD INDEX idx(col), ALGORITHM=INPLACE;
   ```""",
        },
    }

    DEFAULT_GUIDE = {
        "title": "알 수 없는 이슈",
        "description": "이 이슈에 대한 자동 가이드가 없습니다.",
        "solution": "MySQL 공식 문서를 참고하거나 DBA에게 문의하세요.",
    }

    def __init__(self, issues: list, parent=None):
        super().__init__(parent)
        self.issues = issues

        self.setWindowTitle("📖 수동 처리 가이드")
        self.setMinimumSize(700, 500)
        self.init_ui()

    def init_ui(self):
        layout = QVBoxLayout(self)

        # 안내 텍스트
        info_label = QLabel(
            f"다음 {len(self.issues)}개 이슈는 자동 수정이 불가능합니다.\n"
            f"아래 가이드를 참고하여 수동으로 처리하세요."
        )
        info_label.setStyleSheet("margin-bottom: 10px;")
        layout.addWidget(info_label)

        # 스플리터: 이슈 목록 | 가이드 내용
        splitter = QSplitter(Qt.Orientation.Horizontal)

        # 왼쪽: 이슈 목록
        list_widget = QWidget()
        list_layout = QVBoxLayout(list_widget)
        list_layout.setContentsMargins(0, 0, 0, 0)

        list_label = QLabel("이슈 목록")
        list_label.setStyleSheet("font-weight: bold;")
        list_layout.addWidget(list_label)

        self.issue_list = QTableWidget()
        self.issue_list.setColumnCount(2)
        self.issue_list.setHorizontalHeaderLabels(["유형", "위치"])
        self.issue_list.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.issue_list.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.issue_list.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.issue_list.itemSelectionChanged.connect(self.on_issue_selected)
        list_layout.addWidget(self.issue_list)

        splitter.addWidget(list_widget)

        # 오른쪽: 가이드 내용
        guide_widget = QWidget()
        guide_layout = QVBoxLayout(guide_widget)
        guide_layout.setContentsMargins(0, 0, 0, 0)

        self.guide_title = QLabel("가이드")
        self.guide_title.setStyleSheet("font-weight: bold; font-size: 14px;")
        guide_layout.addWidget(self.guide_title)

        self.guide_content = QTextEdit()
        self.guide_content.setReadOnly(True)
        self.guide_content.setStyleSheet("""
            QTextEdit {
                font-family: 'Consolas', 'Monaco', monospace;
                font-size: 12px;
            }
        """)
        guide_layout.addWidget(self.guide_content)

        splitter.addWidget(guide_widget)
        splitter.setSizes([250, 450])

        layout.addWidget(splitter)

        # 닫기 버튼
        btn_layout = QHBoxLayout()
        btn_close = QPushButton("닫기")
        btn_close.clicked.connect(self.accept)
        btn_layout.addStretch()
        btn_layout.addWidget(btn_close)
        layout.addLayout(btn_layout)

        # 이슈 목록 채우기
        self.populate_issues()

    def populate_issues(self):
        """이슈 목록 채우기"""
        self.issue_list.setRowCount(len(self.issues))

        for i, issue in enumerate(self.issues):
            type_name = ISSUE_TYPE_DISPLAY_NAMES.get(issue.issue_type, str(issue.issue_type.value))
            self.issue_list.setItem(i, 0, QTableWidgetItem(type_name))
            self.issue_list.setItem(i, 1, QTableWidgetItem(issue.location))

        # 첫 번째 이슈 선택
        if self.issues:
            self.issue_list.selectRow(0)

    @staticmethod
    def _markdown_to_safe_html(content: str) -> str:
        """가이드 텍스트의 마크다운 서브셋(굵게/코드 펜스/구분선)을 안전한 HTML로 변환.

        위치/설명 등 분석 결과에서 온 텍스트가 섞여 있을 수 있으므로 항상 먼저 이스케이프한다.
        """
        escaped = html.escape(content)

        lines = []
        in_fence = False
        for raw_line in escaped.split("\n"):
            stripped = raw_line.strip()
            if stripped.startswith("```"):
                if in_fence:
                    lines.append("</code></pre>")
                    in_fence = False
                else:
                    lines.append('<pre style="background-color:#f0f0f0; padding:8px;"><code>')
                    in_fence = True
                continue

            if in_fence:
                lines.append(raw_line)
                continue

            if stripped == "---":
                lines.append("<hr>")
                continue

            lines.append(raw_line)

        if in_fence:
            # 닫히지 않은 펜스는 방어적으로 닫는다
            lines.append("</code></pre>")

        result = "\n".join(lines)
        result = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", result)
        return result.replace("\n", "<br>")

    def on_issue_selected(self):
        """이슈 선택 시 가이드 표시"""
        selected = self.issue_list.selectedItems()
        if not selected:
            return

        row = selected[0].row()
        issue = self.issues[row]

        # 가이드 가져오기
        guide = self.GUIDES.get(issue.issue_type, self.DEFAULT_GUIDE)

        self.guide_title.setText(f"📖 {guide['title']}")

        content = f"""**위치:** {issue.location}

**설명:** {issue.description}

---

{guide['solution']}
"""
        self.guide_content.setHtml(self._markdown_to_safe_html(content))
