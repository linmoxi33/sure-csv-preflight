#!/usr/bin/env python3
"""sure-csv-preflight - offline preflight for CSV files imported into Sure.

The rules are a faithful port of Sure's own CSV import code
(app/models/import.rb#sanitize_number, app/models/import/row.rb), so you can see
what the server will do to every cell *before* uploading the file.

Zero third-party dependencies. No network access. Deterministic output.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

TOOL = "sure-csv-preflight"
VERSION = "1.0.0"

# Mirrors Import::NUMBER_FORMATS (app/models/import.rb#L57-L60).
NUMBER_FORMATS = {
    "1,234.56": {"separator": ".", "delimiter": ","},
    "1.234,56": {"separator": ",", "delimiter": "."},
    "1 234,56": {"separator": ",", "delimiter": " "},
    "1,234": {"separator": "", "delimiter": ","},
}

AMOUNT_TYPE_STRATEGIES = ("signed_amount", "custom_column")
SIGNAGE_CONVENTIONS = ("inflows_positive", "inflows_negative")

DEFAULT_CONFIG = {
    "date_col_label": "date",
    "amount_col_label": "amount",
    "name_col_label": "name",
    "currency_col_label": "currency",
    "entity_type_col_label": "entity_type",
    "date_format": "%Y-%m-%d",
    "number_format": "1,234.56",
    "signage_convention": "inflows_positive",
    "amount_type_strategy": "signed_amount",
    "amount_type_inflow_value": None,
    "amount_type_identifier_value": None,
    "col_sep": ",",
    "rows_to_skip": 0,
    "default_currency": "USD",
}

REQUIRED_KEYS = ("date", "amount", "name")

SEVERITY = {
    "HEADER_MISSING": "error",
    "REQUIRED_BLANK": "error",
    "AMOUNT_UNPARSEABLE": "error",
    "DATE_INVALID": "error",
    "NO_DATA_ROWS": "error",
    "AMOUNT_FORMAT_AMBIGUOUS": "warning",
    "AMOUNT_FORMAT_SENSITIVE": "info",
    "ROWS_SKIPPED": "info",
}


class UsageError(Exception):
    """Bad CLI usage, missing file, or invalid configuration (exit code 2)."""


def sanitize_number(raw, number_format):
    """Port of Import#sanitize_number. Returns Decimal or None when rejected."""
    fmt = NUMBER_FORMATS.get(number_format)
    if fmt is None or raw is None:
        return None
    s = str(raw).strip()

    if fmt["delimiter"] == " ":
        # Ruby strips /\p{Space}/; Python's \s on str covers the same set
        # (ASCII spaces, NBSP, narrow no-break space).
        s = re.sub(r"\s", "", s)
        sep = re.escape(fmt["separator"])
        edge = re.compile(r"^[^\d" + sep + r"\-]+|[^\d" + sep + r"\-]+$")
        s = edge.sub("", s)
    else:
        keep = (
            r"[^\d"
            + re.escape(fmt["delimiter"])
            + re.escape(fmt["separator"])
            + r"\-]"
        )
        s = re.sub(keep, "", s)
        if fmt["delimiter"]:
            s = s.replace(fmt["delimiter"], "")

    if fmt["separator"]:
        s = s.replace(fmt["separator"], ".")

    if not re.fullmatch(r"-?\d+\.?\d*", s):
        return None
    try:
        return Decimal(s)
    except InvalidOperation:  # pragma: no cover - regex already guards this
        return None


PLAUSIBLE = {
    # A value is "plausible" under a format when it is a syntactically normal
    # rendering for that format, so a human could have meant either reading.
    "1,234.56": re.compile(r"^-?\d{1,3}(?:,\d{3})*(?:\.\d+)?$|^-?\d+(?:\.\d+)?$"),
    "1.234,56": re.compile(r"^-?\d{1,3}(?:\.\d{3})*(?:,\d+)?$|^-?\d+(?:,\d+)?$"),
    "1 234,56": re.compile(r"^-?\d{1,3}(?: \d{3})*(?:,\d+)?$|^-?\d+(?:,\d+)?$"),
    "1,234": re.compile(r"^-?\d{1,3}(?:,\d{3})*$"),
}


def plausible_formats(raw):
    s = re.sub(r"[^\d., \-]", "", str(raw).strip())
    return [name for name, pattern in PLAUSIBLE.items() if pattern.match(s)]


def all_parses(raw):
    """Every (format, value) pair this cell would parse to under Sure's formats."""
    out = []
    for name in NUMBER_FORMATS:
        value = sanitize_number(raw, name)
        if value is not None:
            out.append((name, value))
    return out


def signed_amount(value, config, entity_type=""):
    """Port of Import::Row#signed_amount for transaction imports."""
    strategy = config["amount_type_strategy"]
    if strategy == "signed_amount":
        factor = -1 if config["signage_convention"] == "inflows_positive" else 1
        return value * factor
    selected = (
        config.get("amount_type_identifier_value")
        or config.get("amount_type_inflow_value")
    )
    legacy = config.get("amount_type_inflow_value")
    inflow_treatment = (
        legacy
        if legacy in SIGNAGE_CONVENTIONS
        else config["signage_convention"]
    )
    if entity_type == selected:
        factor = -1 if inflow_treatment == "inflows_positive" else 1
    else:
        factor = 1 if inflow_treatment == "inflows_positive" else -1
    return value * factor


def parse_date(raw, fmt, today):
    """Port of Import::Row#date_valid (exact strptime + supported range)."""
    try:
        parsed = datetime.strptime(str(raw).strip(), fmt).date()
    except (ValueError, TypeError):
        return None, "date does not match format %s" % fmt
    if parsed < date(1970, 1, 1) or parsed > today:
        return None, "date outside Sure's supported range (1970-01-01..%s)" % today.isoformat()
    return parsed, None


def read_csv_rows(path, text, col_sep):
    if col_sep not in (",", ";"):
        raise UsageError("col_sep must be ',' or ';'")
    reader = csv.reader(text.splitlines(), delimiter=col_sep)
    try:
        rows = list(reader)
    except csv.Error as exc:
        raise UsageError("cannot parse CSV: %s" % exc)
    if not rows:
        raise UsageError("CSV file is empty")
    headers = [h.strip() for h in rows[0]]
    body = rows[1:]
    return headers, body


def load_config(path):
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise UsageError("cannot read config %s: %s" % (path, exc))
    except json.JSONDecodeError as exc:
        raise UsageError("config is not valid JSON: %s" % exc)
    if not isinstance(raw, dict):
        raise UsageError("config must be a JSON object")

    cfg = dict(DEFAULT_CONFIG)
    cfg.update({k: v for k, v in raw.items() if k in DEFAULT_CONFIG})

    if cfg["number_format"] not in NUMBER_FORMATS:
        raise UsageError(
            "number_format must be one of: %s" % ", ".join(NUMBER_FORMATS)
        )
    if cfg["signage_convention"] not in SIGNAGE_CONVENTIONS:
        raise UsageError("signage_convention must be one of: %s" % ", ".join(SIGNAGE_CONVENTIONS))
    if cfg["amount_type_strategy"] not in AMOUNT_TYPE_STRATEGIES:
        raise UsageError(
            "amount_type_strategy must be one of: %s" % ", ".join(AMOUNT_TYPE_STRATEGIES)
        )
    if not isinstance(cfg["rows_to_skip"], int) or cfg["rows_to_skip"] < 0:
        raise UsageError("rows_to_skip must be a non-negative integer")
    if not isinstance(cfg["date_format"], str) or not cfg["date_format"].startswith("%"):
        raise UsageError("date_format must be a strptime pattern such as %Y-%m-%d")
    return cfg


def column_label(cfg, key):
    return cfg.get(key + "_col_label") or key


def find_header(headers, label):
    """Port of Import#header_for: exact, then case-insensitive match."""
    if label in headers:
        return label
    lowered = {h.lower(): h for h in headers}
    return lowered.get(str(label).lower())


def preflight(csv_path, cfg, today=None):
    today = today or date.today()
    path = Path(csv_path)
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise UsageError("CSV is not valid UTF-8 (Sure transcodes uploads; convert first)")
    except OSError as exc:
        raise UsageError("cannot read CSV %s: %s" % (csv_path, exc))
    if not text.strip():
        raise UsageError("CSV file is empty")

    headers, body = read_csv_rows(path, text, cfg["col_sep"])
    findings = []
    summary = {
        "rows_total": len(body),
        "rows_skipped": min(cfg["rows_to_skip"], len(body)),
        "rows_checked": max(0, len(body) - cfg["rows_to_skip"]),
        "rows_importable": 0,
        "rows_rejected": 0,
        "amounts_unparseable": 0,
        "amounts_ambiguous": 0,
        "amounts_sensitive": 0,
        "dates_invalid": 0,
        "signed_total": "0",
        "findings": 0,
    }

    def add(code, row, column, value, message, witness=None):
        item = {
            "code": code,
            "severity": SEVERITY[code],
            "row": row,
            "column": column,
            "value": value,
            "message": message,
        }
        if witness:
            item["witness"] = witness
        findings.append(item)

    labels = {}
    for key in REQUIRED_KEYS + ("currency", "entity_type"):
        label = column_label(cfg, key)
        header = find_header(headers, label)
        if header is None and key in REQUIRED_KEYS:
            add(
                "HEADER_MISSING",
                0,
                label,
                label,
                "mapped column %r is not in the CSV header; Sure resolves it to nil and "
                "every row fails the %s requirement" % (label, key),
                {"headers": headers},
            )
        labels[key] = header

    if len(body) - cfg["rows_to_skip"] <= 0:
        add("NO_DATA_ROWS", 0, None, None, "no data rows left after rows_to_skip")

    data_rows = body[cfg["rows_to_skip"]:]
    for offset, row in enumerate(data_rows):
        row_number = offset + 1 + cfg["rows_to_skip"]
        # Sure's source_row_number is 1-based over data rows, not file lines.
        problem = False
        entity = ""
        if labels["entity_type"] is not None:
            idx = headers.index(labels["entity_type"])
            entity = row[idx].strip() if idx < len(row) else ""

        amount_value = None
        if labels["amount"] is not None:
            idx = headers.index(labels["amount"])
            raw_amount = row[idx].strip() if idx < len(row) else ""
            if raw_amount == "":
                add("REQUIRED_BLANK", row_number, cfg["amount_col_label"], raw_amount,
                    "amount cell is empty; Sure rejects the row")
                problem = True
            else:
                amount_value = sanitize_number(raw_amount, cfg["number_format"])
                if amount_value is None:
                    summary["amounts_unparseable"] += 1
                    problem = True
                    add(
                        "AMOUNT_UNPARSEABLE",
                        row_number,
                        cfg["amount_col_label"],
                        raw_amount,
                        "sanitize_number() rejects this cell, Sure stores a blank amount and "
                        "the row fails validation",
                        {"number_format": cfg["number_format"]},
                    )
                alternatives = [
                    {"number_format": name, "value": str(value)}
                    for name, value in all_parses(raw_amount)
                    if value != amount_value
                ]
                distinct = {str(amount_value)} | {a["value"] for a in alternatives}
                if len(distinct) > 1:
                    code = (
                        "AMOUNT_FORMAT_AMBIGUOUS"
                        if len(plausible_formats(raw_amount)) > 1
                        else "AMOUNT_FORMAT_SENSITIVE"
                    )
                    if code == "AMOUNT_FORMAT_AMBIGUOUS":
                        summary["amounts_ambiguous"] += 1
                    else:
                        summary["amounts_sensitive"] += 1
                    add(
                        code,
                        row_number,
                        cfg["amount_col_label"],
                        raw_amount,
                        "this cell parses differently under another of Sure's four number "
                        "formats; the configured format wins silently",
                        {
                            "chosen": cfg["number_format"],
                            "chosen_value": None if amount_value is None else str(amount_value),
                            "alternatives": alternatives,
                            "ratio": _ratio(amount_value, alternatives),
                        },
                    )

        if labels["date"] is not None:
            idx = headers.index(labels["date"])
            raw_date = row[idx].strip() if idx < len(row) else ""
            if raw_date == "":
                add("REQUIRED_BLANK", row_number, cfg["date_col_label"], raw_date,
                    "date cell is empty; Sure rejects the row")
                problem = True
            else:
                parsed, err = parse_date(raw_date, cfg["date_format"], today)
                if parsed is None:
                    summary["dates_invalid"] += 1
                    problem = True
                    add("DATE_INVALID", row_number, cfg["date_col_label"], raw_date, err,
                        {"date_format": cfg["date_format"]})

        if labels["name"] is not None:
            idx = headers.index(labels["name"])
            raw_name = row[idx].strip() if idx < len(row) else ""
            if raw_name == "":
                add("REQUIRED_BLANK", row_number, cfg["name_col_label"], raw_name,
                    "name cell is empty; Sure rejects the row")
                problem = True

        if not problem:
            summary["rows_importable"] += 1
            if amount_value is not None:
                summary["signed_total"] = str(
                    Decimal(summary["signed_total"]) + signed_amount(amount_value, cfg, entity)
                )
        else:
            summary["rows_rejected"] += 1

    if cfg["rows_to_skip"] > 0:
        add(
            "ROWS_SKIPPED",
            0,
            None,
            cfg["rows_to_skip"],
            "%d leading data row(s) are skipped before mapping" % cfg["rows_to_skip"],
        )

    findings.sort(key=lambda f: (f["row"], f["column"] or "", f["code"]))
    summary["findings"] = len(findings)
    return {
        "tool": TOOL,
        "version": VERSION,
        "source": {
            "path": str(path),
            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "bytes": len(text.encode("utf-8")),
        },
        "config": {k: cfg[k] for k in sorted(DEFAULT_CONFIG)},
        "headers": headers,
        "summary": summary,
        "findings": findings,
    }


def _ratio(chosen, alternatives):
    if chosen is None or not alternatives:
        return None
    try:
        alt = Decimal(alternatives[0]["value"])
        if alt == 0 or chosen == 0:
            return None
        return str((chosen / alt).normalize())
    except (InvalidOperation, KeyError):
        return None


def render_text(report):
    s = report["summary"]
    lines = [
        "%s %s" % (report["tool"], report["version"]),
        "source: %s (%d bytes, sha256 %s)" % (
            report["source"]["path"], report["source"]["bytes"], report["source"]["sha256"][:12]
        ),
        "config: number_format=%s date_format=%s signage=%s strategy=%s sep=%r skip=%d" % (
            report["config"]["number_format"],
            report["config"]["date_format"],
            report["config"]["signage_convention"],
            report["config"]["amount_type_strategy"],
            report["config"]["col_sep"],
            report["config"]["rows_to_skip"],
        ),
        "rows: %d total, %d checked, %d importable, %d rejected" % (
            s["rows_total"], s["rows_checked"], s["rows_importable"], s["rows_rejected"]
        ),
        "amounts: %d unparseable, %d ambiguous, %d format-sensitive | dates: %d invalid | signed total: %s" % (
            s["amounts_unparseable"], s["amounts_ambiguous"], s["amounts_sensitive"],
            s["dates_invalid"], s["signed_total"]
        ),
    ]
    if not report["findings"]:
        lines.append("no findings")
        return "\n".join(lines)
    lines.append("")
    for f in report["findings"]:
        where = "row %d" % f["row"] if f["row"] else "file"
        lines.append("%-7s %-24s %s col=%s value=%r" % (
            f["severity"].upper(), f["code"], where, f["column"], f["value"]
        ))
        lines.append("        %s" % f["message"])
        w = f.get("witness") or {}
        if f["code"] in ("AMOUNT_FORMAT_AMBIGUOUS", "AMOUNT_FORMAT_SENSITIVE"):
            lines.append("        chosen %s -> %s ; " % (
                w.get("chosen"), w.get("chosen_value")
            ) + " ; ".join(
                "%s -> %s" % (a["number_format"], a["value"]) for a in w.get("alternatives", [])
            ))
    return "\n".join(lines)


def build_parser():
    p = argparse.ArgumentParser(
        prog=TOOL,
        description="Offline preflight for CSV files imported into Sure.",
    )
    p.add_argument("--csv", required=True, help="path to the bank CSV export")
    p.add_argument("--config", help="JSON config mirroring Sure's import parameters")
    p.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    p.add_argument(
        "--fail-on",
        choices=("error", "warning", "never"),
        default="warning",
        help="exit 1 at this severity or above (default: warning)",
    )
    p.add_argument("--today", help="override today's date (YYYY-MM-DD) for reproducibility")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        cfg = load_config(args.config) if args.config else dict(DEFAULT_CONFIG)
        today = date.fromisoformat(args.today) if args.today else date.today()
        report = preflight(args.csv, cfg, today=today)
    except UsageError as exc:
        if args.json:
            json.dump({"tool": TOOL, "error": str(exc)}, sys.stdout, ensure_ascii=False, indent=2)
            sys.stdout.write("\n")
        else:
            sys.stderr.write("%s: error: %s\n" % (TOOL, exc))
        return 2
    except ValueError as exc:
        sys.stderr.write("%s: error: %s\n" % (TOOL, exc))
        return 2

    if args.json:
        json.dump(report, sys.stdout, ensure_ascii=False, indent=2, sort_keys=False)
        sys.stdout.write("\n")
    else:
        sys.stdout.write(render_text(report) + "\n")

    if args.fail_on == "never":
        return 0
    if args.fail_on == "warning":
        return 1 if any(f["severity"] in ("error", "warning") for f in report["findings"]) else 0
    return 1 if any(f["severity"] == "error" for f in report["findings"]) else 0


if __name__ == "__main__":
    sys.exit(main())
