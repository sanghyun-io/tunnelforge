from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROPOSAL = PROJECT_ROOT / "docs" / "product_maturity_proposal_2026-07-15.html"
STATUS = PROJECT_ROOT / "docs" / "current_status.md"


class ProposalParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: list[str] = []
        self.anchor_targets: list[str] = []
        self.local_assets: list[str] = []
        self.html_lang = ""
        self.has_viewport = False
        self.has_title = False
        self.has_h1 = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        element_id = attributes.get("id")
        if element_id:
            self.ids.append(element_id)

        if tag == "html":
            self.html_lang = attributes.get("lang", "") or ""
        elif tag == "meta" and attributes.get("name") == "viewport":
            self.has_viewport = True
        elif tag == "title":
            self.has_title = True
        elif tag == "h1":
            self.has_h1 = True
        elif tag == "a":
            href = attributes.get("href", "") or ""
            if href.startswith("#"):
                self.anchor_targets.append(href[1:])
        elif tag in {"img", "script", "link"}:
            location = attributes.get("src") or attributes.get("href") or ""
            if location and not location.startswith(("http://", "https://", "data:")):
                self.local_assets.append(location)


def _parse_proposal() -> tuple[str, ProposalParser]:
    text = PROPOSAL.read_text(encoding="utf-8")
    parser = ProposalParser()
    parser.feed(text)
    return text, parser


def test_product_maturity_proposal_has_complete_accessible_structure():
    text, parser = _parse_proposal()

    assert text.lstrip().lower().startswith("<!doctype html>")
    assert parser.html_lang == "ko"
    assert parser.has_viewport
    assert parser.has_title
    assert parser.has_h1
    assert len(parser.ids) == len(set(parser.ids))
    assert set(parser.anchor_targets) <= set(parser.ids)
    assert "@media (max-width: 640px)" in text
    assert "@media print" in text


def test_product_maturity_proposal_tracks_consensus_and_required_sections():
    text, _ = _parse_proposal()

    required_content = [
        "Safety and Proof",
        "팀 합의",
        "근거 기준선",
        "범위 결정표",
        "확인된 결함과 출구 조건",
        "재설계 전 실행 계약",
        "90일 실행 순서",
        "폐기 가능한 DB 워크플로 검증",
        "조건부 6개월 로드맵",
        "위험 관리",
        "TF-STATUS-095",
        "TF-STATUS-096",
        "TF-STATUS-097",
        "TF-STATUS-098",
        "TF-STATUS-099",
        "TF-STATUS-100",
        "TF-STATUS-101",
        "다운로드 수를 사용자 수로 환산하지 않는다",
    ]

    for content in required_content:
        assert content in text

    assert "TODO" not in text
    assert "TBD" not in text
    assert "lorem ipsum" not in text.lower()


def test_product_maturity_proposal_local_assets_exist():
    _, parser = _parse_proposal()

    for asset in parser.local_assets:
        resolved = (PROPOSAL.parent / unquote(asset)).resolve()
        assert resolved.is_file(), f"Missing local proposal asset: {asset}"


def test_current_status_tracks_product_maturity_decision_and_latest_release():
    status = STATUS.read_text(encoding="utf-8")
    summary = status.split("## Summary", maxsplit=1)[1].split(
        "## Current Baseline Verification", maxsplit=1
    )[0]

    assert "The latest stable release is now `v2.4.0`" in summary
    assert "`v2.4.0` supersedes it as stable/latest" in summary
    assert "`v2.3.1` is published as the latest stable" not in summary
    assert "`docs/product_maturity_proposal_2026-07-15.html`" in summary

    expected_rows = {
        "TF-STATUS-094": "closed",
        "TF-STATUS-095": "closed",
        "TF-STATUS-096": "closed",
        "TF-STATUS-097": "open",
        "TF-STATUS-098": "open",
        "TF-STATUS-099": "open",
        "TF-STATUS-100": "open",
        "TF-STATUS-101": "open",
    }
    for issue_id, state in expected_rows.items():
        assert f"| {issue_id} |" in status
        row = next(line for line in status.splitlines() if line.startswith(f"| {issue_id} |"))
        assert f"| {state} |" in row

    recommended = status.split("## Recommended Execution Order", maxsplit=1)[1].split(
        "## Session Log", maxsplit=1
    )[0]
    for issue_id in ("TF-STATUS-095", "TF-STATUS-096", "TF-STATUS-097", "TF-STATUS-098"):
        assert issue_id in recommended
