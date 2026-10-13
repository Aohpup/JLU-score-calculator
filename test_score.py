import tempfile
from io import BytesIO
import unittest
import sys
import json
import threading
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request, urlopen

import pandas as pd
import score

from score import (
    BrowserLifetime,
    BUILTINS,
    TemplateStore,
    builtin_selected,
    calculate_selection,
    courses_from_dataframe,
    require_windows_webview2,
)


class ScoreCalculatorTests(unittest.TestCase):
    def setUp(self):
        self.courses = courses_from_dataframe(
            pd.DataFrame(
                [
                    ("2025-2026", "高等数学", "必修", 3.5, 90, 4.0),
                    ("2025-2026", "军事理论", "必修", 2.0, 92, 4.0),
                    ("2025-2026", "军事训练", "必修", 2.0, "优秀", 4.0),
                    ("2025-2026", "通用学术英语I", "必修", 2.0, 87, 3.7),
                    ("2025-2026", "体育Ⅰ", "必修", 1.0, 84, 3.3),
                    ("2025-2026", "大学生心理健康", "限选", 2.0, 99, 4.0),
                    ("2025-2026", "艺术鉴赏", "选修", 1.0, "优秀", 4.0),
                ],
                columns=["学年学期", "课程名", "课程性质", "学分", "总成绩", "绩点"],
            )
        )

    def test_builtin_selection_and_weighted_results(self):
        self.assertEqual([(item["id"], item["name"]) for item in BUILTINS], [
            ("postgraduate", "保研加权（不含限选课程）"), ("scholarship", "奖学金加权"), ("comprehensive", "所有课程")
        ])
        scholarship = [builtin_selected("scholarship", c) for c in self.courses]
        postgraduate = [builtin_selected("postgraduate", c) for c in self.courses]
        comprehensive = [builtin_selected("comprehensive", c) for c in self.courses]
        self.assertEqual([c.name for c, chosen in zip(self.courses, scholarship) if chosen], ["高等数学", "军事理论"])
        self.assertEqual([c.name for c, chosen in zip(self.courses, postgraduate) if chosen], ["高等数学"])
        self.assertEqual(sum(comprehensive), len(self.courses))
        result = calculate_selection(self.courses, scholarship)
        self.assertEqual(result["credits"], 5.5)
        self.assertAlmostEqual(result["weighted_score"], (90 * 3.5 + 92 * 2) / 5.5)
        self.assertEqual(self.courses[2].score, 95)

    def test_aliases_year_term_and_course_nature_variants(self):
        frame = pd.DataFrame({
            "课程名称": ["数学", "物理", "艺术"],
            "得分（百分制）": ["88分", "92", "优秀"],
            "课程学分": [3, 4, 2],
            "课程类型": ["专业必修课", "限定选修", "任选课"],
            "成绩绩点": [3.7, 4.0, 4.0],
            "学年": ["2024-2025"] * 3,
            "学期": [1, 1, 2],
        })
        courses = courses_from_dataframe(frame)
        self.assertEqual([c.score for c in courses], [88, 92, 95])
        self.assertEqual(courses[0].term, "2024-2025 / 1")
        self.assertEqual([builtin_selected("postgraduate", c) for c in courses], [True, False, False])
        self.assertTrue(courses[0].as_dict()["required"])

    def test_missing_optional_fields_does_not_invent_gpa(self):
        courses = courses_from_dataframe(pd.DataFrame({
            "Course Name": ["Mathematics"], "Mark": [85], "Credits": [3],
        }))
        self.assertEqual(courses[0].nature, "未标注")
        self.assertFalse(courses[0].gpa_valid)
        result = calculate_selection(courses, [True])
        self.assertIsNone(result["weighted_gpa"])
        self.assertEqual(result["weighted_score"], 85)

    def test_score_inference_ignores_identifiers_and_rejects_ambiguity(self):
        frame = pd.DataFrame({
            "科目": ["数学", "物理", "英语"], "学分数": [3, 2, 4],
            "ID": [81, 82, 83], "记录值": [86, 78, 93],
        })
        courses = courses_from_dataframe(frame)
        self.assertEqual([c.score for c in courses], [86, 78, 93])
        self.assertTrue(any("根据数据特征" in note for note in courses.import_notes))
        frame["其他值"] = [65, 76, 88]
        with self.assertRaisesRegex(ValueError, "多个可能的成绩列"):
            courses_from_dataframe(frame)

    def test_excel_title_rows_and_first_recognizable_sheet(self):
        buffer = BytesIO()
        with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
            pd.DataFrame([["说明"], ["请见成绩表"]]).to_excel(writer, sheet_name="封面", header=False, index=False)
            pd.DataFrame([
                ["另一所学校成绩表", None, None],
                ["课程名称", "分数", "学分"],
                ["数学", 90, 3], ["物理", 80, 2],
            ]).to_excel(writer, sheet_name="学业成绩", header=False, index=False)
        courses = score.courses_from_bytes("grades.xlsx", buffer.getvalue())
        self.assertEqual([c.name for c in courses], ["数学", "物理"])
        self.assertIn("学业成绩", courses.import_notes[0])

    def test_semicolon_csv_with_title_and_legacy_encoding(self):
        data = "成绩单\n科目名称;成绩;学分;GPA;学期\n数学;90;3;4;秋季\n物理;55;2;0;春季\n".encode("gb18030")
        courses = score.courses_from_bytes("grades.csv", data)
        self.assertEqual([c.term for c in courses], ["秋季", "春季"])
        self.assertEqual([c.gpa for c in courses], [4, 0])

    def test_policy_notice_confirmation_persists_without_changing_templates(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict("os.environ", {"APPDATA": directory}):
                first = score.AppState()
                template = first.templates.save("本学院方案", "postgraduate", [], [])
                self.assertFalse(first.snapshot()["policy_notice_seen"])
                first.preferences.acknowledge_policy()
                reopened = score.AppState()
                self.assertTrue(reopened.snapshot()["policy_notice_seen"])
                self.assertEqual(reopened.templates.load(), [template])

    def test_policy_notice_api_saves_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict("os.environ", {"APPDATA": directory}):
                state = score.AppState()
                server = score.ThreadingHTTPServer(("127.0.0.1", 0), score.make_handler(state, Path(score.__file__).with_name("index.html")))
                worker = threading.Thread(target=server.serve_forever, daemon=True)
                worker.start()
                try:
                    url = f"http://127.0.0.1:{server.server_port}/api/policy-notice"
                    request = Request(url, data=json.dumps({"acknowledged": True}).encode(), headers={"X-Score-Token": state.token, "Content-Type": "application/json"})
                    with urlopen(request) as response:
                        self.assertTrue(json.load(response)["policy_notice_seen"])
                    self.assertTrue(score.AppState().snapshot()["policy_notice_seen"])
                finally:
                    server.shutdown()
                    server.server_close()
                    worker.join(3)

    def test_custom_template_is_saved_as_base_plus_overrides(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TemplateStore(Path(directory) / "templates.json")
            template = store.save(
                "只计军事理论和心理健康",
                "scholarship",
                [self.courses[5].key],
                [self.courses[0].key],
            )
            loaded = TemplateStore(store.path).load()[0]
            self.assertEqual(loaded, template)
            self.assertEqual(loaded["base"], "scholarship")
            self.assertEqual(loaded["include"], [self.courses[5].key])
            self.assertEqual(loaded["exclude"], [self.courses[0].key])
            store.delete(template["id"])
            self.assertEqual(store.load(), [])

    def test_qualitative_grades_use_displayed_score_values(self):
        frame = pd.DataFrame(
            [(grade, "必修", 1, 0) for grade in ("优秀", "良好", "中等", "及格", "不及格")],
            columns=["总成绩", "课程性质", "学分", "绩点"],
        )
        frame["课程名"] = [f"课程{index}" for index in range(5)]
        courses = courses_from_dataframe(frame)
        self.assertEqual([course.score for course in courses], [95, 85, 75, 65, 55])

    @unittest.skipUnless(sys.platform == "win32", "Windows only")
    def test_missing_webview2_reports_a_clear_error(self):
        with patch("winreg.OpenKey", side_effect=FileNotFoundError):
            with self.assertRaisesRegex(RuntimeError, "WebView2 Runtime"):
                require_windows_webview2()

    def test_browser_lifetime_ends_after_last_page_closes(self):
        with patch("score.time.monotonic", return_value=100):
            lifetime = BrowserLifetime()
            lifetime.visit("first-page")
        with patch("score.time.monotonic", return_value=101):
            lifetime.close("first-page")
        with patch("score.time.monotonic", return_value=102):
            self.assertFalse(lifetime.should_stop())
        with patch("score.time.monotonic", return_value=103):
            self.assertTrue(lifetime.should_stop())

    def test_closing_browser_page_stops_the_server(self):
        opened = threading.Event()
        urls = []

        def record_url(url):
            urls.append(url)
            opened.set()
            return True

        with tempfile.TemporaryDirectory() as directory:
            with patch.dict("os.environ", {"APPDATA": directory}):
                with patch("score.webbrowser.open", side_effect=record_url):
                    with patch.object(sys, "argv", ["score.py", "--browser"]):
                        worker = threading.Thread(target=score.main, daemon=True)
                        worker.start()
                        self.assertTrue(opened.wait(5))
                        client = "test-browser-page"
                        with urlopen(Request(urls[0] + "api/state", headers={"X-Score-Client": client})) as response:
                            token = json.load(response)["token"]
                        close = Request(
                            urls[0] + "api/page-close", data=b"closed",
                            headers={"X-Score-Client": client, "X-Score-Token": token},
                        )
                        with urlopen(close) as response:
                            self.assertEqual(json.load(response), {"ok": True})
                        worker.join(6)
                        self.assertFalse(worker.is_alive())


if __name__ == "__main__":
    unittest.main()
