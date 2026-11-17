#!/usr/bin/env python3
"""Check labels.csv against its saved public-source manifest."""

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import io
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit


SOURCE_PATH = re.compile(r"^/r/ChatGPT/comments/([^/]+)/")
REDDIT_ID = re.compile(r"^t3_[a-z0-9]+$")


def read_inputs(data_path, sources_path):
    """Load both files and retain their exact bytes for reproducible hashes."""
    data_bytes = Path(data_path).read_bytes()
    sources_bytes = Path(sources_path).read_bytes()

    text = data_bytes.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text, newline=""))
    if not reader.fieldnames or len(reader.fieldnames) != len(set(reader.fieldnames)):
        raise ValueError(f"{data_path} has a missing or duplicate CSV header.")
    if "text" not in reader.fieldnames:
        raise ValueError(f"{data_path} has no 'text' column.")
    rows = list(reader)
    if any(row.get("text") is None or None in row for row in rows):
        raise ValueError(f"{data_path} contains a row that does not match its header.")

    manifest = json.loads(sources_bytes.decode("utf-8"))
    if not isinstance(manifest, dict) or not isinstance(manifest.get("posts"), list):
        raise ValueError(f"{sources_path} must contain a JSON object with a 'posts' list.")
    return rows, manifest["posts"], data_bytes, sources_bytes


def text_character_count(text):
    """Match the manifest's UTF-16 count independent of CSV line endings."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return len(normalized.encode("utf-16-le")) // 2


def validate(rows, sources):
    """Return row-specific mapping diagnostics for a dataset and its manifest."""
    errors = []
    seen_ids = {}
    seen_urls = {}
    paired_count = min(len(rows), len(sources))

    for index in range(paired_count):
        expected_row = index + 1
        source = sources[index]
        if not isinstance(source, dict):
            errors.append({"row": expected_row, "reason": "source record is not an object"})
            continue

        if type(source.get("row")) is not int or source["row"] != expected_row:
            errors.append({
                "row": expected_row,
                "reason": f"source record at position {expected_row} records row {source.get('row')!r}",
            })

        source_id = source.get("id")
        source_url = source.get("source_url")
        if not isinstance(source_id, str) or not REDDIT_ID.fullmatch(source_id):
            errors.append({"row": expected_row, "reason": "source ID is missing or malformed"})
        else:
            if source_id in seen_ids:
                errors.append({
                    "row": expected_row,
                    "reason": f"duplicate source ID {source_id!r} (first seen at row {seen_ids[source_id]})",
                })
            else:
                seen_ids[source_id] = expected_row

        if not isinstance(source_url, str):
            errors.append({"row": expected_row, "reason": "source URL is missing or malformed"})
        else:
            if source_url in seen_urls:
                errors.append({
                    "row": expected_row,
                    "reason": f"duplicate source URL (first seen at row {seen_urls[source_url]})",
                })
            else:
                seen_urls[source_url] = expected_row

            try:
                parsed = urlsplit(source_url)
                valid_origin = (
                    parsed.scheme == "https"
                    and parsed.hostname == "www.reddit.com"
                    and parsed.username is None
                    and parsed.password is None
                    and parsed.port is None
                )
                match = SOURCE_PATH.match(parsed.path)
            except ValueError:
                valid_origin = False
                match = None
            if not valid_origin or not match:
                errors.append({
                    "row": expected_row,
                    "reason": "source URL must be an https://www.reddit.com/r/ChatGPT/comments/{id}/ URL",
                })
            elif isinstance(source_id, str) and match.group(1) != source_id.removeprefix("t3_"):
                errors.append({
                    "row": expected_row,
                    "reason": f"source URL post ID {match.group(1)!r} does not match source ID {source_id!r}",
                })

        recorded_length = source.get("text_characters")
        actual_length = text_character_count(rows[index]["text"])
        if type(recorded_length) is not int or recorded_length != actual_length:
            errors.append({
                "row": expected_row,
                "reason": (
                    f"text_characters is {recorded_length!r}; "
                    f"CSV text has {actual_length} UTF-16 code units"
                ),
            })

    missing_mappings = max(0, len(rows) - len(sources))
    for row_number in range(paired_count + 1, len(rows) + 1):
        errors.append({"row": row_number, "reason": "missing source record"})

    extra_mappings = max(0, len(sources) - len(rows))
    for source_position in range(paired_count, len(sources)):
        source = sources[source_position]
        recorded_row = source.get("row") if isinstance(source, dict) else None
        errors.append({
            "row": recorded_row if isinstance(recorded_row, int) else None,
            "reason": f"extra source record at position {source_position + 1}",
        })

    return {
        "status": "PASS" if not errors else "FAIL",
        "csv_rows": len(rows),
        "source_records": len(sources),
        "missing_mappings": missing_mappings,
        "extra_mappings": extra_mappings,
        "errors": errors,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Check dataset rows against their saved Reddit source manifest."
    )
    parser.add_argument("--data", default="labels.csv", help="labelled dataset CSV")
    parser.add_argument(
        "--sources", default="data/labels_sources.json", help="saved source manifest"
    )
    parser.add_argument(
        "--out", default="provenance_guard_results.json", help="JSON report path"
    )
    parser.add_argument(
        "--log", default="provenance_guard_runs.jsonl", help="append-only JSONL run log"
    )
    args = parser.parse_args()

    started_at = time.perf_counter()
    timestamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )
    run = {
        "timestamp_utc": timestamp,
        "event": "provenance_guard.run",
        "input": {"data": args.data, "sources": args.sources},
        "output": {"report": args.out, "status": "not_written"},
    }

    try:
        rows, sources, data_bytes, sources_bytes = read_inputs(args.data, args.sources)
    except (OSError, UnicodeError, csv.Error, json.JSONDecodeError, ValueError) as exc:
        run["status"] = "error"
        run["error"] = f"{type(exc).__name__}: {exc}"
        exit_code = 2
        print(f"Cannot check provenance: {exc}", file=sys.stderr)
    else:
        run["input"]["sha256"] = {
            "data": hashlib.sha256(data_bytes).hexdigest(),
            "sources": hashlib.sha256(sources_bytes).hexdigest(),
        }
        report = validate(rows, sources)
        report["input_sha256"] = run["input"]["sha256"]
        run["details"] = {
            "csv_rows": report["csv_rows"],
            "source_records": report["source_records"],
            "validation_errors": len(report["errors"]),
        }

        try:
            Path(args.out).write_text(
                json.dumps(report, indent=2) + "\n", encoding="utf-8"
            )
        except OSError as exc:
            run["status"] = "error"
            run["error"] = f"{type(exc).__name__}: {exc}"
            exit_code = 2
            print(f"Cannot write report to {args.out}: {exc}", file=sys.stderr)
        else:
            run["status"] = "pass" if report["status"] == "PASS" else "fail"
            run["output"]["status"] = report["status"]
            exit_code = 0 if report["status"] == "PASS" else 1

    run["duration_seconds"] = round(time.perf_counter() - started_at, 6)
    try:
        with Path(args.log).open("a", encoding="utf-8") as log_file:
            log_file.write(json.dumps(run, ensure_ascii=False, sort_keys=True) + "\n")
    except OSError as exc:
        print(f"Cannot append run log to {args.log}: {exc}", file=sys.stderr)
        return 2
    if run["status"] in ("pass", "fail"):
        print(
            f"{run['output']['status']}: {run['details']['csv_rows']} CSV rows, "
            f"{run['details']['source_records']} source records, "
            f"{run['details']['validation_errors']} errors"
        )
        print(f"Report: {args.out}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
