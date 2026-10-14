"""Import 전 확인 창: dump.import_preview 결과로 무엇을 덮어쓰고 새로 만드는지 한 번에 보여 준다."""
from typing import Dict, List, Tuple

from PyQt6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea, QVBoxLayout

from src.core.error_report_sanitizer import sanitize_local_diagnostic
from src.core.i18n import translate_text
from src.core.production_guard import ENV_NAMES, Environment, SchemaConfirmDialog
from src.exporters.rust_dump_exporter import CORE_CODE_SENTENCES, describe_copy_blockers, describe_core_code

# Only these export policies lack a common point in time (import.rs coded_source_dump_warnings).
_UNSYNCHRONIZED_POLICIES = ("mysql_parallel_unsynchronized_snapshots",
                            "mysql_parallel_no_backup_lock_consistent_snapshot")
_UNKNOWN_BLOCKER = "진행할 수 없는 다른 이유가 있습니다. '자세히'에서 확인하세요."
# Before the run there is no Import report yet: the core's text is behind '자세히'.
_UNKNOWN_COPY_STOP = "복사본에서 검증할 수 없는 다른 이유가 있습니다. '자세히'에서 확인하세요."
_LIST_MAX_HEIGHT = 160  # a long name list scrolls instead of pushing the buttons off the screen


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


def _target_word(preview: dict) -> str:
    """What the user calls the target: a MySQL DB or a PostgreSQL schema."""
    target = preview.get("target") if isinstance(preview.get("target"), dict) else {}
    return "스키마" if target.get("engine") == "postgresql" else "DB"


def describe_plan(preview: dict, verify: bool, timezone_label: str = "") -> Dict:
    """Korean lines for a dump.import_preview result.

    ``blockers`` stop the run, ``warnings`` are shown, ``details`` sit behind '자세히'. ``must_see`` is
    whether the run needs the confirmation even when it overwrites nothing: a blocker, or a warning that
    the imported data itself may be off.
    """
    # A blocker the application cannot read still blocks (fail closed).
    raw_blockers = preview.get("blockers") if isinstance(preview.get("blockers"), list) else []
    blockers = describe_copy_blockers([item if isinstance(item, dict) else {} for item in raw_blockers],
                                      unknown=_UNKNOWN_BLOCKER)
    copy_stops = describe_copy_blockers(_dicts(preview.get("copy_blockers")), unknown=_UNKNOWN_COPY_STOP) if verify else []
    if copy_stops:
        blockers += copy_stops + [translate_text("'먼저 복사본에서 검증하고 교체'를 끄면 진행할 수 있습니다.")]

    data_warnings, notes = [], []
    views = preview.get("views") if isinstance(preview.get("views"), dict) else {}
    parents = _dicts(preview.get("external_parents"))

    def fk(item):
        return f"{item.get('child')}.{item.get('constraint')} → {item.get('parent')}"

    for template, names in (
        ("교체하면 사라지는 트리거: {}" if verify else "덮어쓰면 사라지는 트리거: {}",
         [f"{item.get('table')}.{item.get('name')}" for item in _dicts(preview.get("triggers_dropped"))]),
        ("이 계정으로 지우지 못할 수 있는 뷰(다른 계정이 만듦): {}",
         [f"{item.get('name')} ({item.get('definer')})" for item in _dicts(views.get("may_not_replace"))]),
        ("교체 뒤 동작하지 않을 수 있는 뷰: {}", [f"{item.get('view')} → {item.get('table')}" for item in _dicts(views.get("may_break"))]),
        ("덤프에 없는 부모 테이블을 가리키는 외래 키입니다. 부모 테이블에 맞는 행이 있는지는 확인하지 않습니다: {}",
         [fk(item) for item in parents if item.get("exists_in_target") is not False]),
        ("가리키는 부모 테이블이 덤프에도 대상에도 없는 외래 키입니다: {}",
         [fk(item) for item in parents if item.get("exists_in_target") is False]),
    ):
        if names:
            data_warnings.append(_line(template, names))
    for code in dict.fromkeys(str(item.get("code") or "") for item in _dicts(preview.get("warnings"))):
        sentence = describe_core_code(code) or translate_text(CORE_CODE_SENTENCES["source_dump_warning"])
        if code == "unsynchronized_snapshot":
            if preview.get("snapshot_policy") in _UNSYNCHRONIZED_POLICIES:
                data_warnings.append(f"- {sentence}")
        elif code == "legacy_timezone":
            # Worded here: the core's sentence cannot know whether a timezone was chosen.
            if timezone_label:
                notes.append("- " + translate_text("예전 덤프라 시간대가 기록되어 있지 않아, 고른 시간대({})로 시간을 읽습니다.")
                             .format(timezone_label))
            else:
                data_warnings.append("- " + translate_text(
                    "예전 덤프라 시간대가 기록되어 있지 않아, 대상 서버의 기본 시간대로 시간을 읽습니다. "
                    "다른 시간대를 쓰려면 고급 설정의 '시간대 직접 정하기'에서 고르세요."))
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
        items = preview.get(key) if isinstance(preview.get(key), list) else []
        details += [sanitize_local_diagnostic(f"{item.get('code')}: {item.get('detail')}" if isinstance(item, dict) else str(item))
                    for item in items]
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
        views = preview.get("views") if isinstance(preview.get("views"), dict) else {}
        replaced_views = _strings(views.get("replace"))
        word = _target_word(preview)
        # Development gets no colored warning.
        colors = ("#333", "#fff") if environment == Environment.DEVELOPMENT else self.ENV_COLORS.get(environment, ("#333", "#fff"))
        self.setWindowTitle(translate_text("Import 전 확인"))
        self.setMinimumWidth(520)
        self.setModal(True)

        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        if environment != Environment.DEVELOPMENT:
            layout.addWidget(self._build_header_frame(*colors))
        if not replace and replaced_views:
            warning = translate_text("기존 뷰 {}개를 바꾸고, 나머지는 새로 만듭니다").format(len(replaced_views))
        elif not replace:
            warning = translate_text("새로 만들기만 합니다")
        elif plan["verify"]:
            warning = translate_text(f"교체 전 테이블은 백업 {word}에 남습니다")
        else:
            warning = translate_text("덮어쓴 테이블은 되돌릴 수 없습니다")
        self.label_warning = QLabel("⚠️ " + warning)
        self.label_warning.setStyleSheet(f"color: {colors[0]}; font-weight: bold;")
        layout.addWidget(self.label_warning)
        new_target = translate_text(" (새로 만듭니다)") if target.get("exists") is False else ""
        layout.addWidget(QLabel(translate_text("넣을 곳: {}").format(sanitize_local_diagnostic(target_text)) + new_target))
        if toggles:
            layout.addWidget(QLabel(toggles))

        self.lists = {}
        for key, title, names in (
                ("replace", "교체할 테이블 {}개" if plan["verify"] else "덮어쓸 테이블 {}개", replace),
                ("create", "새로 만들 테이블 {}개", create),
                ("untouched", "그대로 둘 테이블 {}개 (덤프에 없음)", untouched)):
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
            widgets = self._build_schema_input_section(*colors)
            # The base dialog says 스키마명 on MySQL too; this one names what the user types.
            widgets[1].setText(translate_text(f"계속하려면 <b>{word} 이름을 똑같이 입력</b>하세요:"))
            self.input_schema.setPlaceholderText(translate_text(f"{word} 이름 입력"))
            for widget in widgets:
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
        area = QScrollArea()
        area.setWidget(label)
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.Shape.NoFrame)
        area.setMaximumHeight(_LIST_MAX_HEIGHT)
        area.setVisible(False)
        layout.addWidget(area)
        button.clicked.connect(lambda: area.setVisible(area.isHidden()))
        return button, label
