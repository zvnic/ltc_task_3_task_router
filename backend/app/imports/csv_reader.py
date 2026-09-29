import csv
import hashlib
import io
import re
import unicodedata
from collections import Counter
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.core.errors import DomainError

REQUIRED_REQUEST_COLUMNS = {
    "Заявка",
    "Тип заявки BK",
    "Тип заявки HD",
    "Начало",
    "Окончание",
    "Район",
    "Адрес",
    "Гигабитное подключение",
}
MOSCOW = ZoneInfo("Europe/Moscow")


def decode_csv(content: bytes) -> tuple[str, str]:
    for encoding in ("utf-8-sig", "cp1251"):
        try:
            return content.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    raise DomainError("unsupported_encoding", "CSV должен быть UTF-8 или Windows-1251.")


def normalize_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).strip().casefold().split())


def normalize_building_address(value: str) -> str:
    normalized = normalize_text(value)
    return re.sub(r",\s*кв\.?\s*\d+\s*$", "", normalized)


def parse_datetime(value: str, line_number: int) -> datetime:
    try:
        return datetime.strptime(value.strip(), "%d.%m.%Y %H:%M").replace(tzinfo=MOSCOW)
    except ValueError as exc:
        raise DomainError(
            "invalid_datetime",
            "Некорректная дата или время в CSV.",
            details={"line": line_number, "value": value},
        ) from exc


def read_rows(content: bytes) -> tuple[list[dict[str, str]], str, str]:
    text, encoding = decode_csv(content)
    first_line = text.splitlines()[0] if text.splitlines() else ""
    delimiter = ";" if ";" in first_line else "," if "," in first_line else ""
    if not delimiter:
        raise DomainError("unknown_delimiter", "Не удалось определить разделитель CSV.")
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    if reader.fieldnames is None or not REQUIRED_REQUEST_COLUMNS.issubset(reader.fieldnames):
        raise DomainError(
            "unknown_schema",
            "CSV не соответствует схеме заявок.",
            details={"columns": reader.fieldnames or []},
        )
    return [dict(row) for row in reader], encoding, delimiter


def parse_requests_csv(content: bytes) -> dict[str, Any]:
    if len(content) > 5 * 1024 * 1024:
        raise DomainError("file_too_large", "Размер файла превышает 5 MiB.", status_code=413)
    rows, encoding, delimiter = read_rows(content)
    valid_rows: list[dict[str, Any]] = []
    skipped_empty = 0
    office_address: str | None = None
    errors: list[dict[str, Any]] = []
    seen_id_lines: dict[str, int] = {}
    for index, row in enumerate(rows, start=2):
        stripped = {key: (value or "").strip() for key, value in row.items()}
        if not any(stripped.values()):
            skipped_empty += 1
            continue
        external_id = stripped["Заявка"]
        if normalize_text(external_id) == normalize_text("Адрес Офиса"):
            office_address = stripped["Тип заявки BK"]
            continue
        if not external_id:
            errors.append({"line": index, "code": "missing_id"})
            continue
        if external_id in seen_id_lines:
            errors.append(
                {
                    "line": index,
                    "code": "duplicate_id",
                    "external_id": external_id,
                    "first_line": seen_id_lines[external_id],
                }
            )
            continue
        seen_id_lines[external_id] = index
        try:
            window_start = parse_datetime(stripped["Начало"], index)
            window_end = parse_datetime(stripped["Окончание"], index)
        except DomainError as exc:
            errors.append({"line": index, "code": exc.code, **exc.details})
            continue
        if window_start > window_end:
            errors.append({"line": index, "code": "window_reversed"})
            continue
        if stripped["Гигабитное подключение"] not in {"Да", "Нет"}:
            errors.append({"line": index, "code": "invalid_gigabit"})
            continue
        valid_rows.append(
            {
                "line": index,
                "input_order": len(valid_rows),
                "external_id": external_id,
                "source_work_type": stripped["Тип заявки BK"],
                "source_work_subtype": stripped["Тип заявки HD"],
                "window_start": window_start,
                "window_end": window_end,
                "district": stripped["Район"],
                "address_raw": stripped["Адрес"],
                "address_normalized": normalize_building_address(stripped["Адрес"]),
                "connection_type": stripped.get("Подключение") or None,
                "is_gigabit": stripped["Гигабитное подключение"] == "Да",
                "source_fields": stripped,
            }
        )
    return {
        "encoding": encoding,
        "delimiter": delimiter,
        "total_rows": len(rows),
        "valid_count": len(valid_rows),
        "skipped_empty": skipped_empty,
        "office_address": office_address,
        "errors": errors,
        "rows": valid_rows,
        "sha256": hashlib.sha256(content).hexdigest(),
    }


def reference_key(row: dict[str, str]) -> tuple[str, str, str, str, str]:
    return (
        normalize_building_address(row["Адрес"]),
        normalize_reference_datetime(row["Начало"]),
        normalize_reference_datetime(row["Окончание"]),
        normalize_text(row["Тип заявки BK"]),
        normalize_text(row["Тип заявки HD"]),
    )


def normalize_reference_datetime(value: str) -> str:
    try:
        return datetime.strptime(value.strip(), "%d.%m.%Y %H:%M").strftime("%Y-%m-%dT%H:%M")
    except ValueError:
        return normalize_text(value)


def audit_sources(request_content: bytes, control_content: bytes) -> dict[str, Any]:
    parsed = parse_requests_csv(request_content)
    control_rows, control_encoding, control_delimiter = read_rows(control_content)
    source_keys = Counter(
        (
            row["address_normalized"],
            row["window_start"].strftime("%Y-%m-%dT%H:%M"),
            row["window_end"].strftime("%Y-%m-%dT%H:%M"),
            normalize_text(row["source_work_type"]),
            normalize_text(row["source_work_subtype"]),
        )
        for row in parsed["rows"]
    )
    matches = [source_keys[reference_key(row)] for row in control_rows]
    match_issues = [
        {
            "reference_external_id": row["Заявка"].strip(),
            "match_count": match_count,
            "address": row["Адрес"].strip(),
            "window_start": row["Начало"].strip(),
            "window_end": row["Окончание"].strip(),
            "work_type": row["Тип заявки BK"].strip(),
            "work_subtype": row["Тип заявки HD"].strip(),
        }
        for row, match_count in zip(control_rows, matches, strict=True)
        if match_count != 1
    ]
    source_ids = {row["external_id"] for row in parsed["rows"]}
    control_ids = {row["Заявка"].strip() for row in control_rows}
    request_profile = {
        "work_types": dict(
            sorted(Counter(row["source_work_type"] for row in parsed["rows"]).items())
        ),
        "work_subtypes": dict(
            sorted(Counter(row["source_work_subtype"] for row in parsed["rows"]).items())
        ),
        "districts": dict(sorted(Counter(row["district"] for row in parsed["rows"]).items())),
        "dates": dict(
            sorted(
                Counter(row["window_start"].date().isoformat() for row in parsed["rows"]).items()
            )
        ),
        "windows": dict(
            sorted(
                Counter(
                    f"{row['window_start'].strftime('%H:%M')}–{row['window_end'].strftime('%H:%M')}"
                    for row in parsed["rows"]
                ).items()
            )
        ),
        "gigabit_count": sum(row["is_gigabit"] for row in parsed["rows"]),
        "connection_type_missing": sum(row["connection_type"] is None for row in parsed["rows"]),
    }
    control_profile = {
        "statuses": dict(
            sorted(Counter((row.get("Статус BK") or "").strip() for row in control_rows).items())
        ),
        "teams": dict(
            sorted(
                Counter(
                    (row.get("Бригада") or "").strip() or "<не назначено>" for row in control_rows
                ).items()
            )
        ),
    }
    return {
        "requests": {
            **{key: value for key, value in parsed.items() if key != "rows"},
            "profile": request_profile,
        },
        "control": {
            "encoding": control_encoding,
            "delimiter": control_delimiter,
            "rows": len(control_rows),
            "unique_matches": sum(value == 1 for value in matches),
            "ambiguous": sum(value > 1 for value in matches),
            "unmatched": sum(value == 0 for value in matches),
            "id_intersection": len(source_ids & control_ids),
            "match_issues": match_issues,
            "profile": control_profile,
        },
    }
