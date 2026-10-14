"""Import 전 확인 창: dump.import_preview 결과로 무엇을 덮어쓰고 새로 만드는지 한 번에 보여 준다."""
from typing import Dict, List, Tuple

from PyQt6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from src.core.error_report_sanitizer import sanitize_local_diagnostic
from src.core.i18n import translate_text
from src.core.production_guard import ENV_NAMES, Environment, SchemaConfirmDialog
from src.exporters.rust_dump_exporter import CORE_CODE_SENTENCES, describe_copy_blockers, describe_core_code

# Only these export policies lack a common point in time (import.rs coded_source_dump_warnings).
_UNSYNCHRONIZED_POLICIES = ("mysql_parallel_unsynchronized_snapshots",
                            "mysql_parallel_no_backup_lock_consistent_snapshot")
_UNKNOWN_BLOCKER = "진행할 수 없는 다른 이유가 있습니다. '자세히'에서 확인하세요."


def names_preview(names: list, limit: int = 10) -> str:
    shown = ", ".join(str(name) for name in names[:limit])
    if len(names) > limit:
        shown += translate_text(" 외 {}개").format(len(names) - limit)
    return shown


def _dicts(value) -> List[dict]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _strings(value) -> List[str]:
    return [str(item) for item in value if isinstance(item, (str, int))] if isinstance(value, list) else []


def _line(template: str, names: List[str]) -> str:
    return "- " + translate_text(template).format(sanitize_local_diagnostic(names_preview(names)))


def describe_plan(preview: dict, verify: bool, timezone_label: str = "") -> Dict:
    """Korean lines for a dump.import_preview result.

    ``blockers`` stop the run, ``warnings`` are shown, ``details`` sit behind '자세히'. ``must_see`` is
    whether the run needs the confirmation even when it overwrites nothing: a blocker, or a warning that
    the imported data itself may be off.
    """
    blockers = describe_copy_blockers(_dicts(preview.get("blockers")), unknown=_UNKNOWN_BLOCKER)
    copy_stops = describe_copy_blockers(_dicts(preview.get("copy_blockers"))) if verify else []
    if copy_stops:
        blockers += copy_stops + [translate_text("'먼저 복사본에서 검증하고 교체'를 끄면 진행할 수 있습니다.")]

    data_warnings, notes = [], []
    views = preview.get("views") if isinstance(preview.get("views"), dict) else {}
    for template, names in (
        ("교체하면 사라지는 트리거: {}", [f"{item.get('table')}.{item.get('name')}" for item in _dicts(preview.get("triggers_dropped"))]),
        ("이 계정으로 지우지 못할 수 있는 뷰(다른 계정이 만듦): {}",
         [f"{item.get('name')} ({item.get('definer')})" for item in _dicts(views.get("may_not_replace"))]),
        ("교체 뒤 동작하지 않을 수 있는 뷰: {}", [f"{item.get('view')} → {item.get('table')}" for item in _dicts(views.get("may_break"))]),
        ("부모 행과 맞는지 확인하지 않습니다: {}",
         [f"{item.get('child')}.{item.get('constraint')} → {item.get('parent')}" for item in _dicts(preview.get("external_parents"))]),
    ):
        if names:
            data_warnings.append(_line(template, names))
    for code in dict.fromkeys(str(item.get("code") or "") for item in _dicts(preview.get("warnings"))):
        sentence = describe_core_code(code) or translate_text(CORE_CODE_SENTENCES["source_dump_warning"])
        if code == "unsynchronized_snapshot":
            if preview.get("snapshot_policy") in _UNSYNCHRONIZED_POLICIES:
                data_warnings.append(f"- {sentence}")
        elif code == "legacy_timezone":
            choice = (translate_text("고른 시간대: {}").format(timezone_label) if timezone_label
                      else translate_text("고급 설정의 '타임존 직접 정하기'에서 고를 수 있습니다."))
            (notes if timezone_label else data_warnings).append(f"- {sentence} {choice}")
        else:
            notes.append(f"- {sentence}")

    details = []
    children = [f"{item.get('table')}.{item.get('constraint')} → {item.get('parent')}" for item in _dicts(preview.get("kept_children"))]
    if children:
        details.append(_line("그대로 두는 대상 테이블의 외래 키: {}", children))
    replace_views, create_views = _strings(views.get("replace")), _strings(views.get("create"))
    if replace_views or create_views:
        details.append("- " + translate_text("교체할 뷰 {}개, 새로 만들 뷰 {}개").format(len(replace_views), len(create_views)))
    for key in ("blockers", "copy_blockers", "warnings"):
        details += [sanitize_local_diagnostic(f"{item.get('code')}: {item.get('detail')}") for item in _dicts(preview.get(key))]
    return {"blockers": blockers, "warnings": data_warnings + notes, "details": details, "verify": verify,
            "must_see": bool(blockers or data_warnings)}


class ImportPlanDialog(SchemaConfirmDialog):
    """The one confirmation of an Import run. Production still asks for the name to be typed."""

    ENV_LABELS = ENV_NAMES

    def __init__(self, parent, preview: dict, plan: Dict, environment: Environment, target_text: str, name: str,
                 toggles: str = ""):
        super().__init__(parent, "데이터 Import", name, environment)
        self.blockers = plan["blockers"]
        replace, create, untouched = (_strings(preview.get(key)) for key in ("replace", "create", "untouched"))
        target = preview.get("target") if isinstance(preview.get("target"), dict) else {}
        # Development gets no colored warning.
        colors = ("#333", "#fff") if environment == Environment.DEVELOPMENT else self.ENV_COLORS.get(environment, ("#333", "#fff"))
        self.setWindowTitle(translate_text("Import 전 확인"))
        self.setMinimumWidth(520)
        self.setModal(True)

        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        if environment != Environment.DEVELOPMENT:
            layout.addWidget(self._build_header_frame(*colors))
        if not replace:
            warning = "새로 만들기만 합니다"
        elif plan["verify"]:
            warning = "교체 전 테이블은 백업 DB에 남습니다"
        else:
            warning = "덮어쓴 테이블은 되돌릴 수 없습니다"
        self.label_warning = QLabel("⚠️ " + translate_text(warning))
        self.label_warning.setStyleSheet(f"color: {colors[0]}; font-weight: bold;")
        layout.addWidget(self.label_warning)
        new_target = translate_text(" (새로 만듭니다)") if target.get("exists") is False else ""
        layout.addWidget(QLabel(translate_text("넣을 곳: {}").format(sanitize_local_diagnostic(target_text)) + new_target))
        if toggles:
            layout.addWidget(QLabel(toggles))

        self.lists = {}
        for key, title, names in (("replace", "덮어쓸 테이블 {}개", replace), ("create", "새로 만들 테이블 {}개", create),
                                  ("untouched", "건드리지 않을 테이블 {}개", untouched)):
            self.lists[key] = self._expandable(layout, translate_text(title).format(len(names)),
                                               "\n".join(sanitize_local_diagnostic(name) for name in names))
        for key, title, color in (("blockers", "진행할 수 없는 이유", "#c0392b"), ("warnings", "주의할 점", "#b45309")):
            if plan[key]:
                heading = QLabel(translate_text(title))
                heading.setStyleSheet(f"color: {color}; font-weight: bold;")
                body = QLabel("\n".join(plan[key]))
                body.setWordWrap(True)
                layout.addWidget(heading)
                layout.addWidget(body)
                setattr(self, f"label_{key}", body)
        if plan["details"]:
            self.details = self._expandable(layout, translate_text("자세히"), "\n".join(plan["details"]))

        if environment == Environment.PRODUCTION:
            for widget in self._build_schema_input_section(*colors):
                layout.addWidget(widget)
        layout.addLayout(self._build_button_row(colors[0]))
        if environment != Environment.PRODUCTION:
            self.btn_execute.setEnabled(not self.blockers)
        if environment in (Environment.STAGING, Environment.UNKNOWN):
            self.btn_execute.setAutoDefault(False)
            self.btn_cancel.setDefault(True)
            self.btn_cancel.setFocus()

    def _init_ui(self, operation: str, details: str):
        """Built in __init__ once the plan is known."""

    def _on_text_changed(self, text: str):
        self.btn_execute.setEnabled(not self.blockers and text.strip() == self.schema_name)

    @staticmethod
    def _expandable(layout, title: str, body: str) -> Tuple[QPushButton, QLabel]:
        """A title that opens its list below it; a title without a list is plain text."""
        row = QHBoxLayout()
        button = QPushButton(title + (" ▸" if body else ""))
        button.setFlat(True)
        button.setEnabled(bool(body))
        row.addWidget(button)
        row.addStretch()
        layout.addLayout(row)
        label = QLabel(body)
        label.setWordWrap(True)
        label.setVisible(False)
        layout.addWidget(label)
        button.clicked.connect(lambda: label.setVisible(label.isHidden()))
        return button, label
