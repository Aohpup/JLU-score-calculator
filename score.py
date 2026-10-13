"""Local grade calculator with selectable courses and reusable templates."""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO, StringIO
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import csv
import re
import unicodedata
import json
import os
from pathlib import Path
import secrets
import sys
import tempfile
import threading
import time
from urllib.parse import unquote, urlparse
import uuid
import webbrowser

import pandas as pd


GRADE_VALUES = {"优秀": 95, "良好": 85, "中等": 75, "及格": 65, "不及格": 55}
BUILTINS = (
    {"id": "postgraduate", "name": "保研加权（不含限选课程）", "description": "剔除选修、限选、体育、通用学术英语和军事课程"},
    {"id": "scholarship", "name": "奖学金加权", "description": "必修课，剔除英语、体育、军事训练"},
    {"id": "comprehensive", "name": "所有课程", "description": "全部有学分的课程"},
)
BUILTIN_IDS = {item["id"] for item in BUILTINS}
REQUIRED_COLUMNS = ("课程名", "总成绩", "学分")
COLUMN_ALIASES = {
    "课程名": ("课程名", "课程名称", "科目", "科目名称", "课程", "教学科目", "course", "coursename", "coursetitle", "subject", "subjectname"),
    "总成绩": ("总成绩", "成绩", "分数", "得分", "最终成绩", "课程成绩", "考试成绩", "综合成绩", "总评", "总评成绩", "总分", "score", "mark", "marks", "grade", "finalscore", "finalgrade", "result"),
    "课程性质": ("课程性质", "性质", "课程类型", "课程类别", "修读类型", "修读性质", "课程属性", "类别", "coursetype", "type", "nature", "category"),
    "学分": ("学分", "课程学分", "修读学分", "学分数", "credit", "credits", "coursecredits"),
    "绩点": ("绩点", "课程绩点", "成绩绩点", "gradepoint", "gradepoints", "gpa", "gp"),
    "学年学期": ("学年学期", "学期", "开课学期", "修读学期", "修读时间", "学期名称", "semester", "term", "academicterm"),
    "学年": ("学年", "学年度", "academic year", "academicyear", "year"),
}
MAX_UPLOAD_BYTES = 25 * 1024 * 1024


@dataclass(frozen=True)
class Course:
    key: str
    term: str
    name: str
    nature: str
    grade: str
    score: float
    credit: float
    gpa: float
    score_valid: bool
    gpa_valid: bool = True

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "term": self.term,
            "name": self.name,
            "nature": self.nature,
            "grade": self.grade,
            "score": self.score,
            "credit": self.credit,
            "gpa": self.gpa,
            "score_valid": self.score_valid,
            "gpa_valid": self.gpa_valid,
            "required": nature_category(self.nature) == "必修",
            "builtin": {item["id"]: builtin_selected(item["id"], self) for item in BUILTINS},
        }


def clean_text(value: object) -> str:
    return "" if pd.isna(value) else str(value).strip()


def number_or_zero(value: object) -> float:
    try:
        result = pd.to_numeric(value, errors="coerce")
        return 0.0 if pd.isna(result) else float(result)
    except (TypeError, ValueError):
        return 0.0


class ImportedCourses(list):
    """Course list carrying explanations for the import UI."""

    def __init__(self, notes: list[str]):
        super().__init__()
        self.import_notes = notes


def normalize_header(value: object) -> str:
    text = unicodedata.normalize("NFKC", clean_text(value)).lower()
    text = re.sub(r"\([^)]*\)|\[[^]]*\]", "", text)
    return re.sub(r"[\s_\-:：/]+", "", text)


def header_mapping(columns) -> dict[str, object]:
    """Match complete aliases; prefer canonical names over other aliases."""
    mapped = {}
    used = set()
    for canonical, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            matches = [column for column in columns if column not in used and normalize_header(column) == normalize_header(alias)]
            if len(matches) == 1:
                mapped[canonical] = matches[0]
                used.add(matches[0])
                break
            if len(matches) > 1:
                raise ValueError(f"存在多个同名的{canonical}列，请区分列名后重新导入")
    return mapped


def parse_score(value: object) -> float | None:
    text = unicodedata.normalize("NFKC", clean_text(value))
    if text in GRADE_VALUES:
        return float(GRADE_VALUES[text])
    if re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)\s*分?", text):
        result = float(text.rstrip("分").strip())
        if 0 <= result <= 100:
            return result
    return None


def resolve_columns(frame: pd.DataFrame) -> tuple[dict, list[str]]:
    if frame.columns.duplicated().any():
        raise ValueError("成绩单有重复列名，请为每列设置不同的名称")
    mapping = header_mapping(frame.columns)
    notes = []
    if "总成绩" not in mapping:
        candidates = []
        excluded_words = ("学号", "编号", "代码", "序号", "排名", "学时", "课时", "电话", "id", "rank", "hour", "code", "number", "平时", "期中", "期末", "考试", "补考", "重修")
        for column in frame.columns:
            if column in mapping.values() or any(word in normalize_header(column) for word in excluded_words):
                continue
            values = [clean_text(value) for value in frame[column] if clean_text(value)]
            parsed = [parse_score(value) for value in values]
            valid = [value for value in parsed if value is not None]
            # A two-digit column alone is insufficient: require multiple rows,
            # grade-shaped contents and a single unambiguous candidate.
            if len(valid) >= 3 and len(valid) / len(values) >= .8 and pd.Series(valid).median() >= 50:
                candidates.append(column)
        if len(candidates) == 1:
            mapping["总成绩"] = candidates[0]
            notes.append(f"根据数据特征将“{candidates[0]}”识别为成绩列，请核对导入后的成绩。")
        elif len(candidates) > 1:
            raise ValueError("发现多个可能的成绩列：" + "、".join(map(str, candidates)) + "。请将最终成绩列改名为“成绩”或“总成绩”。")
    missing = [name for name in REQUIRED_COLUMNS if name not in mapping]
    if missing:
        raise ValueError("无法识别成绩单字段：" + "、".join(missing) + "。支持课程名称/科目、总成绩/分数/得分、学分等列名。")
    recognized = [f"{column}→{field}" for field, column in mapping.items() if field in REQUIRED_COLUMNS and str(column) != field]
    if recognized:
        notes.append("已识别字段：" + "、".join(recognized))
    if "课程性质" not in mapping:
        notes.append("未提供课程性质，显示为“未标注”；请按实际课程要求手动选择。")
    if "绩点" not in mapping:
        notes.append("未提供绩点，绩点显示为“—”；请使用本校成绩单中的绩点，不自动套用吉林大学换算。")
    return mapping, notes


def courses_from_dataframe(frame: pd.DataFrame) -> list[Course]:
    mapping, notes = resolve_columns(frame)
    courses = ImportedCourses(notes)
    occurrences: dict[tuple[str, str, str], int] = {}
    for _, row in frame.iterrows():
        name = clean_text(row[mapping["课程名"]])
        if not name or name in {"合计", "总计", "汇总", "平均", "平均成绩"}:
            continue
        def field(key: str) -> str:
            return clean_text(row[mapping[key]]) if key in mapping else ""
        term = field("学年学期")
        year = field("学年")
        if year and year not in term:
            term = f"{year} / {term}" if term else year
        nature = field("课程性质") or "未标注"
        grade = field("总成绩")
        parsed_score = parse_score(grade)
        gpa = pd.to_numeric(field("绩点"), errors="coerce")
        gpa_valid = not pd.isna(gpa) and float(gpa) >= 0
        signature = (term, name, nature)
        ordinal = occurrences.get(signature, 0) + 1
        occurrences[signature] = ordinal
        key = json.dumps([term, name, nature, ordinal], ensure_ascii=False, separators=(",", ":"))
        courses.append(Course(
            key=key, term=term, name=name, nature=nature, grade=grade,
            score=parsed_score if parsed_score is not None else 0.0,
            credit=number_or_zero(field("学分")),
            gpa=float(gpa) if gpa_valid else 0.0,
            score_valid=parsed_score is not None, gpa_valid=gpa_valid,
        ))
    if not courses:
        raise ValueError("已识别表头，但没有找到课程记录")
    return courses


def frame_from_raw(raw: pd.DataFrame) -> pd.DataFrame:
    """Find a header below school titles and student information."""
    raw = raw.dropna(axis=1, how="all")
    for index in range(min(30, len(raw))):
        headers = [clean_text(value) or f"未命名列{position + 1}" for position, value in enumerate(raw.iloc[index])]
        mapping = header_mapping(headers)
        if "课程名" in mapping and "学分" in mapping:
            frame = raw.iloc[index + 1:].copy()
            frame.columns = headers
            return frame.dropna(how="all")
    raise ValueError("未找到成绩单表头；请确保有课程名称、成绩和学分列")


def courses_from_bytes(filename: str, data: bytes) -> list[Course]:
    suffix = Path(filename).suffix.lower()
    if suffix in {".xlsx", ".xls"}:
        with pd.ExcelFile(BytesIO(data)) as workbook:
            errors = []
            for sheet in workbook.sheet_names:
                raw = workbook.parse(sheet, header=None)
                try:
                    courses = courses_from_dataframe(frame_from_raw(raw))
                except ValueError as error:
                    errors.append(f"{sheet}：{error}")
                    continue
                if len(workbook.sheet_names) > 1:
                    courses.import_notes.insert(0, f"已读取工作表“{sheet}”。")
                return courses
            raise ValueError("未找到可导入的成绩表。" + "；".join(errors[:3]))
    if suffix == ".csv":
        for encoding in ("utf-8-sig", "gb18030"):
            try:
                text = data.decode(encoding)
                break
            except UnicodeError:
                continue
        else:
            raise ValueError("无法识别 CSV 文件编码")
        # Try common separators, including exported tab-separated CSV files.
        errors = []
        for delimiter in (",", ";", "\t"):
            rows = list(csv.reader(StringIO(text), delimiter=delimiter))
            try:
                return courses_from_dataframe(frame_from_raw(pd.DataFrame(rows)))
            except ValueError as error:
                errors.append(error)
        raise ValueError(str(max(errors, key=lambda error: len(str(error)))))
    raise ValueError("请选择 .xlsx、.xls 或 .csv 成绩单")


def load_courses(file_path: str | Path) -> list[Course]:
    path = Path(file_path)
    if path.stat().st_size > MAX_UPLOAD_BYTES:
        raise ValueError("成绩单超过 25 MB")
    return courses_from_bytes(path.name, path.read_bytes())


def nature_category(nature: str) -> str:
    normalized = re.sub(r"\s+", "", nature).lower()
    if normalized in {"required", "compulsory", "mandatory"} or "必修" in normalized:
        return "必修"
    if normalized in {"restricted", "restrictedelective"} or "限选" in normalized or "限定选修" in normalized or "限定性选修" in normalized:
        return "限选"
    if normalized in {"elective", "optional"} or "选修" in normalized or "任选" in normalized:
        return "选修"
    return nature


def builtin_selected(template_id: str, course: Course) -> bool:
    if course.credit <= 0:
        return False
    if template_id == "comprehensive":
        return True
    if template_id == "scholarship":
        return nature_category(course.nature) == "必修" and not any(
            word in course.name for word in ("英语", "体育", "军事训练")
        )
    if template_id == "postgraduate":
        return nature_category(course.nature) not in {"选修", "限选"} and not any(
            word in course.name for word in ("通用学术英语", "体育", "军事理论", "军事训练")
        )
    raise ValueError("未知的内置模板")


def calculate_selection(courses: list[Course], selected: list[bool]) -> dict:
    if len(courses) != len(selected):
        raise ValueError("课程和选择数量不一致")
    included = [course for course, checked in zip(courses, selected) if checked and course.credit > 0]
    credits = sum(course.credit for course in included)
    score_sum = sum(course.score * course.credit for course in included)
    gpa_sum = sum(course.gpa * course.credit for course in included)
    return {
        "count": len(included),
        "credits": credits,
        "weighted_score": score_sum / credits if credits else 0.0,
        "weighted_gpa": None if any(not course.gpa_valid for course in included) else (gpa_sum / credits if credits else 0.0),
        "score_sum": score_sum,
        "invalid_scores": sum(not course.score_valid for course in included),
    }


def calculate_gpa(file_path: str | Path) -> str:
    """Keep the original text-report entry point available for other scripts."""
    try:
        courses = load_courses(file_path)
        report = ["===== 成绩计算报告 ====="]
        for item in BUILTINS:
            result = calculate_selection(
                courses, [builtin_selected(item["id"], course) for course in courses]
            )
            report.extend(
                [
                    "",
                    f"【{item['name']}】（{item['description']}）",
                    f"总计学分: {result['credits']:.1f}",
                    "加权绩点: " + (f"{result['weighted_gpa']:.4f}" if result["weighted_gpa"] is not None else "—（有课程未提供绩点）"),
                    f"加权成绩: {result['weighted_score']:.5f}",
                ]
            )
        return "\n".join(report)
    except Exception as error:
        return f"读取或计算时发生错误：{error}"


def default_template_path() -> Path:
    appdata = os.environ.get("APPDATA")
    base = Path(appdata) if appdata else Path.home() / ".config"
    return base / "ScoreCalculator" / "templates.json"


class TemplateStore:
    def __init__(self, path: Path | None = None):
        self.path = path or default_template_path()

    def load(self) -> list[dict]:
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if data.get("version") != 1 or not isinstance(data.get("templates"), list):
                raise ValueError("模板文件格式不正确")
            return [item for item in data["templates"] if isinstance(item, dict)]
        except (OSError, ValueError, TypeError) as error:
            raise ValueError(f"无法读取自定义模板：{error}") from error

    def _write(self, templates: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps({"version": 1, "templates": templates}, ensure_ascii=False, indent=2)
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=self.path.parent, suffix=".tmp", delete=False
        ) as handle:
            temporary_path = Path(handle.name)
            handle.write(content)
        try:
            os.replace(temporary_path, self.path)
        finally:
            temporary_path.unlink(missing_ok=True)

    def save(self, name: str, base: str, include: list[str], exclude: list[str]) -> dict:
        name = name.strip()
        if not 1 <= len(name) <= 40:
            raise ValueError("模板名称需为 1 到 40 个字符")
        if base not in BUILTIN_IDS:
            raise ValueError("请选择一个内置模板作为基础")
        if not isinstance(include, list) or not isinstance(exclude, list):
            raise ValueError("模板课程选择无效")
        if any(not isinstance(key, str) for key in include + exclude):
            raise ValueError("模板课程标识无效")
        if set(include) & set(exclude):
            raise ValueError("同一课程不能同时计入和排除")
        templates = self.load()
        if any(item.get("name", "").casefold() == name.casefold() for item in templates):
            raise ValueError("已有同名模板，请换一个名称")
        template = {
            "id": "custom:" + uuid.uuid4().hex,
            "name": name,
            "base": base,
            "include": sorted(set(include)),
            "exclude": sorted(set(exclude)),
        }
        templates.append(template)
        self._write(templates)
        return template

    def delete(self, template_id: str) -> None:
        templates = self.load()
        kept = [item for item in templates if item.get("id") != template_id]
        if len(kept) == len(templates):
            raise ValueError("没有找到该自定义模板")
        self._write(kept)


class PreferenceStore:
    """Persist first-launch reminders independently of browser ports."""

    def __init__(self, path: Path | None = None):
        self.path = path or default_template_path().with_name("preferences.json")

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def policy_notice_seen(self) -> bool:
        return self._load().get("policy_notice_seen") is True

    def acknowledge_policy(self) -> None:
        data = self._load()
        data["policy_notice_seen"] = True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.path.parent, suffix=".tmp", delete=False) as handle:
            temporary_path = Path(handle.name)
            json.dump(data, handle, ensure_ascii=False, indent=2)
        try:
            os.replace(temporary_path, self.path)
        finally:
            temporary_path.unlink(missing_ok=True)


class BrowserLifetime:
    """Track browser pages so closing the last page ends browser mode."""

    def __init__(self):
        self.lock = threading.Lock()
        self.clients: dict[str, float] = {}
        self.started_at = time.monotonic()
        self.empty_since: float | None = None
        self.ever_connected = False

    def visit(self, client_id: str) -> None:
        if not client_id:
            return
        with self.lock:
            self.clients[client_id] = time.monotonic()
            self.ever_connected = True
            self.empty_since = None

    def close(self, client_id: str) -> None:
        with self.lock:
            self.clients.pop(client_id, None)
            if self.ever_connected and not self.clients:
                self.empty_since = time.monotonic()

    def should_stop(self) -> bool:
        now = time.monotonic()
        with self.lock:
            self.clients = {
                client_id: seen for client_id, seen in self.clients.items()
                if now - seen < 120
            }
            if self.clients:
                return False
            if not self.ever_connected:
                return now - self.started_at >= 60
            if self.empty_since is None:
                self.empty_since = now
            return now - self.empty_since >= 2


class AppState:
    def __init__(self, initial_file: str | None = None):
        self.courses: list[Course] = []
        self.filename = ""
        self.error = ""
        self.templates = TemplateStore()
        self.preferences = PreferenceStore()
        self.token = secrets.token_urlsafe(24)
        self.lock = threading.Lock()
        self.browser_lifetime = BrowserLifetime()
        if initial_file:
            try:
                self.courses = load_courses(initial_file)
                self.filename = Path(initial_file).name
            except Exception as error:
                self.error = f"无法打开成绩单：{error}"

    def snapshot(self) -> dict:
        with self.lock:
            courses = [course.as_dict() for course in self.courses]
            filename = self.filename
            error = self.error
        return {
            "courses": courses,
            "import_notes": getattr(self.courses, "import_notes", []),
            "filename": filename,
            "error": error,
            "builtins": BUILTINS,
            "templates": self.templates.load(),
            "policy_notice_seen": self.preferences.policy_notice_seen(),
            "token": self.token,
        }


def make_handler(state: AppState, html_path: Path):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format_string: str, *args: object) -> None:
            return

        def send_json(self, data: dict, status: int = 200) -> None:
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def read_body(self) -> bytes:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_UPLOAD_BYTES:
                raise ValueError("文件为空或超过 25 MB")
            return self.rfile.read(length)

        def authorized(self) -> bool:
            return secrets.compare_digest(self.headers.get("X-Score-Token", ""), state.token)

        def do_GET(self) -> None:
            route = urlparse(self.path).path
            if route == "/":
                body = html_path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif route == "/assets/jlu_transcript_explanation.jpg":
                image_path = html_path.parent / "jlu_transcript_explanation.jpg"
                try:
                    body = image_path.read_bytes()
                except OSError:
                    self.send_error(404, "帮助图片不存在")
                    return
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif route == "/api/state":
                try:
                    state.browser_lifetime.visit(self.headers.get("X-Score-Client", ""))
                    self.send_json(state.snapshot())
                except ValueError as error:
                    self.send_json({"error": str(error)}, 500)
            else:
                self.send_error(404)

        def do_POST(self) -> None:
            if not self.authorized():
                self.send_json({"error": "请求未授权，请刷新页面"}, 403)
                return
            route = urlparse(self.path).path
            try:
                if route == "/api/import":
                    filename = unquote(self.headers.get("X-Filename", ""))
                    courses = courses_from_bytes(filename, self.read_body())
                    with state.lock:
                        state.courses = courses
                        state.filename = Path(filename).name
                        state.error = ""
                    self.send_json(state.snapshot())
                elif route == "/api/policy-notice":
                    payload = json.loads(self.read_body().decode("utf-8"))
                    if payload.get("acknowledged") is not True:
                        raise ValueError("请确认已阅读学院政策提醒")
                    with state.lock:
                        state.preferences.acknowledge_policy()
                    self.send_json({"policy_notice_seen": True})
                elif route == "/api/templates":
                    payload = json.loads(self.read_body().decode("utf-8"))
                    template = state.templates.save(
                        payload["name"], payload["base"], payload["include"], payload["exclude"]
                    )
                    self.send_json({"template": template, "templates": state.templates.load()})
                elif route == "/api/shutdown":
                    self.send_json({"ok": True})
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
                elif route == "/api/heartbeat":
                    state.browser_lifetime.visit(self.headers.get("X-Score-Client", ""))
                    self.send_json({"ok": True})
                elif route == "/api/page-close":
                    state.browser_lifetime.close(self.headers.get("X-Score-Client", ""))
                    self.send_json({"ok": True})
                else:
                    self.send_error(404)
            except (ValueError, KeyError, OSError) as error:
                self.send_json({"error": str(error)}, 400)

        def do_DELETE(self) -> None:
            if not self.authorized():
                self.send_json({"error": "请求未授权，请刷新页面"}, 403)
                return
            route = urlparse(self.path).path
            if not route.startswith("/api/templates/"):
                self.send_error(404)
                return
            try:
                state.templates.delete(unquote(route.rsplit("/", 1)[1]))
                self.send_json({"templates": state.templates.load()})
            except (ValueError, OSError) as error:
                self.send_json({"error": str(error)}, 400)

    return Handler


class DesktopApi:
    """Close the native window when the page's exit button is pressed."""

    def __init__(self):
        self._window = None

    def close(self) -> None:
        if self._window is not None:
            self._window.destroy()


def require_windows_webview2() -> None:
    """Avoid pywebview silently falling back to the deprecated MSHTML engine."""
    if sys.platform != "win32":
        return

    import winreg

    client = r"SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
    locations = (
        (winreg.HKEY_CURRENT_USER, winreg.KEY_READ),
        (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_READ | winreg.KEY_WOW64_32KEY),
        (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_READ | winreg.KEY_WOW64_64KEY),
    )
    for root, access in locations:
        try:
            with winreg.OpenKey(root, client, 0, access) as key:
                version = str(winreg.QueryValueEx(key, "pv")[0]).strip()
                if version and version != "0.0.0.0":
                    break
        except OSError:
            continue
    else:
        raise RuntimeError("未检测到 Microsoft Edge WebView2 Runtime")

    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full",
        ) as key:
            release = int(winreg.QueryValueEx(key, "Release")[0])
    except (OSError, ValueError, TypeError):
        release = 0
    if release < 394802:
        raise RuntimeError("内嵌窗口需要 .NET Framework 4.6.2 或更新版本")


def offer_browser_fallback(error: Exception) -> bool:
    """Explain a missing desktop renderer and let the user use the web UI."""
    message = (
        "软件窗口无法启动，可能缺少 Microsoft Edge WebView2 Runtime。\n\n"
        f"详细信息：{error}\n\n"
        "选择“是”将在默认浏览器中打开计算器；选择“否”退出。\n"
        "WebView2 下载：https://developer.microsoft.com/microsoft-edge/webview2/"
    )
    if sys.platform == "win32":
        import ctypes

        return ctypes.windll.user32.MessageBoxW(None, message, "成绩与绩点计算器", 0x24) == 6
    print(message, file=sys.stderr)
    return True


def wait_for_browser(server: ThreadingHTTPServer, server_thread: threading.Thread, state: AppState) -> None:
    """Stop even if a browser is closed without using the exit button."""
    while server_thread.is_alive():
        if state.browser_lifetime.should_stop():
            server.shutdown()
            break
        server_thread.join(timeout=0.5)


def main() -> None:
    browser_mode = "--browser" in sys.argv[1:]
    file_args = [arg for arg in sys.argv[1:] if arg != "--browser"]
    initial_file = file_args[0] if file_args else None
    state = AppState(initial_file)
    resource_dir = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    html_path = resource_dir / "index.html"
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state, html_path))
    url = f"http://127.0.0.1:{server.server_port}/"
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    try:
        if browser_mode:
            webbrowser.open(url)
            wait_for_browser(server, server_thread, state)
            return
        try:
            require_windows_webview2()
            import webview

            api = DesktopApi()
            window = webview.create_window(
                "成绩与绩点计算器", url, js_api=api, width=1280, height=820,
                min_size=(760, 560),
            )
            api._window = window
            # Avoid pywebview's deprecated MSHTML fallback on Windows.
            webview.start(gui="edgechromium" if sys.platform == "win32" else None)
        except Exception as error:
            if offer_browser_fallback(error):
                webbrowser.open(url)
                wait_for_browser(server, server_thread, state)
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
