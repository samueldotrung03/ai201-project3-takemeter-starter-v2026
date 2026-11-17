"""Focused tests for the local dataset provenance guard."""

import csv
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import provenance_guard


def fixture_rows():
    return [
        {"text": "first post\nwith a line break 🌱", "label": "analysis", "note": ""},
        {"text": "Second post", "label": "reaction", "note": ""},
    ]


def fixture_sources(rows):
    return [
        {
            "row": index,
            "id": f"t3_{suffix}",
            "source_url": (
                f"https://www.reddit.com/r/ChatGPT/comments/{suffix}/post-title/"
            ),
            "text_characters": provenance_guard.text_character_count(row["text"]),
        }
        for index, (suffix, row) in enumerate(zip(("abc", "def"), rows), start=1)
    ]


class ProvenanceGuardTests(unittest.TestCase):
    def test_valid_mapping_handles_multiline_text_and_hashes_inputs(self):
        rows = fixture_rows()
        sources = fixture_sources(rows)
        with tempfile.TemporaryDirectory() as directory:
            data_path = Path(directory) / "labels.csv"
            sources_path = Path(directory) / "sources.json"
            with data_path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=("text", "label", "note"))
                writer.writeheader()
                writer.writerows(rows)
            sources_path.write_text(json.dumps({"posts": sources}), encoding="utf-8")

            first = provenance_guard.read_inputs(data_path, sources_path)
            second = provenance_guard.read_inputs(data_path, sources_path)
            report = provenance_guard.validate(first[0], first[1])

            self.assertEqual(len(first[0]), 2)
            self.assertEqual(first[0][0]["text"], rows[0]["text"])
            self.assertEqual(report["status"], "PASS")
            self.assertEqual(report["errors"], [])
            self.assertEqual(first[2], second[2])
            self.assertEqual(first[3], second[3])

    def test_missing_and_extra_source_records_fail(self):
        rows = fixture_rows()
        sources = fixture_sources(rows)

        missing = provenance_guard.validate(rows, sources[:1])
        self.assertEqual(missing["missing_mappings"], 1)
        self.assertIn("missing source record", [error["reason"] for error in missing["errors"]])
        self.assertEqual(missing["status"], "FAIL")

        extra = provenance_guard.validate(rows, sources + [dict(sources[-1], row=3, id="t3_ghi")])
        self.assertEqual(extra["extra_mappings"], 1)
        self.assertTrue(
            any("extra source record" in error["reason"] for error in extra["errors"])
        )
        self.assertEqual(extra["status"], "FAIL")

    def test_invalid_source_fields_fail_with_row_diagnostics(self):
        rows = fixture_rows()
        mutations = (
            ("duplicate ID", lambda source: source.update(id="t3_abc"), "duplicate source ID"),
            (
                "duplicate URL",
                lambda source: source.update(
                    source_url="https://www.reddit.com/r/ChatGPT/comments/abc/post-title/"
                ),
                "duplicate source URL",
            ),
            (
                "malformed URL",
                lambda source: source.update(source_url="https://example.com/post"),
                "source URL must be",
            ),
            (
                "mismatched URL ID",
                lambda source: source.update(
                    source_url="https://www.reddit.com/r/ChatGPT/comments/wrong/post-title/"
                ),
                "does not match source ID",
            ),
            (
                "text length",
                lambda source: source.update(text_characters=0),
                "CSV text has",
            ),
            ("row position", lambda source: source.update(row=9), "records row"),
        )

        for name, mutate, expected_reason in mutations:
            with self.subTest(name=name):
                sources = fixture_sources(rows)
                mutate(sources[1])
                report = provenance_guard.validate(rows, sources)
                self.assertEqual(report["status"], "FAIL")
                self.assertTrue(
                    any(
                        error["row"] == 2 and expected_reason in error["reason"]
                        for error in report["errors"]
                    ),
                    report["errors"],
                )

    def test_cli_writes_failure_report_and_returns_nonzero(self):
        rows = fixture_rows()
        sources = fixture_sources(rows)
        sources[1]["text_characters"] = -1

        with tempfile.TemporaryDirectory() as directory:
            data_path = Path(directory) / "labels.csv"
            sources_path = Path(directory) / "sources.json"
            report_path = Path(directory) / "report.json"
            with data_path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=("text", "label", "note"))
                writer.writeheader()
                writer.writerows(rows)
            sources_path.write_text(json.dumps({"posts": sources}), encoding="utf-8")
            output = StringIO()
            with patch(
                "sys.argv",
                [
                    "provenance_guard.py",
                    "--data",
                    str(data_path),
                    "--sources",
                    str(sources_path),
                    "--out",
                    str(report_path),
                ],
            ), redirect_stdout(output):
                exit_code = provenance_guard.main()

            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(exit_code, 1)
            self.assertEqual(report["status"], "FAIL")
            self.assertIn("FAIL:", output.getvalue())


if __name__ == "__main__":
    unittest.main()
