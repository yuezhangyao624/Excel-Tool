from __future__ import annotations

import io
import re
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Iterable

import pandas as pd


NAME_ALIASES = [
    "name",
    "full name",
    "student name",
    "participant name",
    "attendee name",
    "user name",
    "display name",
    "learner name",
    "enrollee name",
    "enrolled name",
    "registrant name",
]

ENROLLMENT_FIELD_ALIASES = [
    "enrollment",
    "learning content enrollment",
]

FIRST_NAME_ALIASES = ["first name", "firstname", "given name"]
LAST_NAME_ALIASES = ["last name", "lastname", "surname", "family name"]

EMAIL_ALIASES = [
    "email",
    "e-mail",
    "email address",
    "e-mail address",
    "participant email",
    "attendee email",
    "user email",
    "registrant email",
]

MINUTES_ALIASES = [
    "minutes",
    "minutes attended",
    "in-meeting duration",
    "in meeting duration",
    "attendance minutes",
    "attended minutes",
    "duration minutes",
    "duration (minutes)",
    "duration",
    "total duration",
    "time attended",
    "time in session",
    "attendance duration",
    "attendance time",
    "actual attendance",
]

HEADER_ALIASES = (
    NAME_ALIASES
    + ENROLLMENT_FIELD_ALIASES
    + FIRST_NAME_ALIASES
    + LAST_NAME_ALIASES
    + EMAIL_ALIASES
    + MINUTES_ALIASES
    + [
        "first join",
        "last leave",
        "join time",
        "leave time",
        "participant id (upn)",
        "role",
        "enrollment status",
        "completion status",
        "enrollment date",
    ]
)


class AttendanceToolError(ValueError):
    """A friendly, user-facing processing error."""


@dataclass(frozen=True)
class AttendanceResult:
    job_id: str
    follow_up: pd.DataFrame
    dropped_enrollments: pd.DataFrame
    all_enrollments: pd.DataFrame
    unmatched_attendance: pd.DataFrame
    summary: dict[str, int | str]
    csv_path: Path
    xlsx_path: Path


def analyze_attendance_files(
    enrollment_bytes: bytes,
    enrollment_filename: str,
    attendance_bytes: bytes,
    attendance_filename: str,
    output_root: Path,
    enrollment_name_column: str = "",
    enrollment_match_column: str = "",
    attendance_match_column: str = "",
    attendance_minutes_column: str = "",
    partial_threshold: float = 30,
    attended_threshold: float = 45,
) -> AttendanceResult:
    if partial_threshold < 0 or attended_threshold <= 0:
        raise AttendanceToolError("Thresholds must be positive numbers.")
    if partial_threshold >= attended_threshold:
        raise AttendanceToolError(
            "The partial-attendance threshold must be lower than the attended threshold."
        )

    enrollments = read_table(enrollment_bytes, enrollment_filename, "enrollment")
    attendance = read_table(attendance_bytes, attendance_filename, "attendance")

    if enrollments.empty:
        raise AttendanceToolError("The enrollment sheet has no rows to process.")
    if attendance.empty:
        raise AttendanceToolError("The actual attendance sheet has no rows to process.")

    enrollment_name = get_display_name_series(enrollments, enrollment_name_column)
    enrollment_original_name = get_name_source_series(
        enrollments, enrollment_name_column, enrollment_name
    )

    enrollment_key_series, attendance_key_series, match_method, key_mode = get_match_series(
        enrollments,
        attendance,
        enrollment_match_column,
        attendance_match_column,
        enrollment_name,
    )

    minutes_col = choose_column(
        attendance,
        attendance_minutes_column,
        MINUTES_ALIASES,
        "actual attendance minutes",
    )
    attendance_minutes = attendance[minutes_col].map(parse_minutes)

    attendance_summary = (
        pd.DataFrame(
            {
                "match_key": attendance_key_series.map(
                    lambda value: normalize_match_key(value, key_mode)
                ),
                "match_alias": attendance_key_series.map(
                    lambda value: normalize_alias_key(value, key_mode)
                ),
                "attendance_name": get_optional_name_series(attendance),
                "minutes_attended": attendance_minutes,
            }
        )
        .query("match_key != ''")
        .groupby("match_key", as_index=False)
        .agg(
            minutes_attended=("minutes_attended", "sum"),
            attendance_name=("attendance_name", first_non_blank),
            match_alias=("match_alias", first_non_blank),
        )
    )

    minutes_by_key = dict(
        zip(attendance_summary["match_key"], attendance_summary["minutes_attended"])
    )
    matches_by_alias = build_unique_alias_lookup(attendance_summary)

    used_keys: set[str] = set()
    rows: list[dict[str, object]] = []
    for sequence, (_, row) in enumerate(enrollments.iterrows(), start=1):
        display_name = clean_display_value(enrollment_name.loc[row.name])
        original_name = clean_display_value(enrollment_original_name.loc[row.name])
        raw_key = enrollment_key_series.loc[row.name]
        match_key = normalize_match_key(raw_key, key_mode)
        match_alias = normalize_alias_key(raw_key, key_mode)
        dropped_info = get_dropped_info(row, original_name)
        minutes = 0.0
        matched_attendance_key = ""
        if match_key and match_key in minutes_by_key:
            minutes = float(minutes_by_key[match_key])
            matched_attendance_key = match_key
        elif key_mode == "name" and match_alias and match_alias in matches_by_alias:
            minutes, matched_attendance_key = matches_by_alias[match_alias]
        if matched_attendance_key:
            used_keys.add(matched_attendance_key)

        status = (
            "Dropped"
            if dropped_info["is_dropped"] and not matched_attendance_key
            else classify_minutes(minutes, partial_threshold, attended_threshold)
        )
        rows.append(
            {
                "Enrollment Sequence": sequence,
                "Name": display_name or clean_display_value(raw_key),
                "Status": status,
                "Minutes Attended": round(minutes, 2),
                "Dropped Reason": dropped_info["reason"],
                "Match Method": match_method,
            }
        )

    all_enrollments = add_row_numbers(sort_by_name(pd.DataFrame(rows)))
    follow_up = all_enrollments[
        all_enrollments["Status"].isin(["Did not attend", "Partially attended"])
    ].copy()
    follow_up = add_row_numbers(follow_up)
    dropped_enrollments = all_enrollments[
        all_enrollments["Status"] == "Dropped"
    ].copy()
    dropped_enrollments = add_row_numbers(dropped_enrollments)

    unmatched = attendance_summary[~attendance_summary["match_key"].isin(used_keys)].copy()
    unmatched_attendance = pd.DataFrame(
        {
            "Name in Attendance Sheet": unmatched["attendance_name"].fillna(""),
            "Minutes Attended": unmatched["minutes_attended"].round(2),
        }
    )

    summary = {
        "Total enrollments": len(all_enrollments),
        "Attended": int((all_enrollments["Status"] == "Attended").sum()),
        "Partially attended": int(
            (all_enrollments["Status"] == "Partially attended").sum()
        ),
        "Did not attend": int((all_enrollments["Status"] == "Did not attend").sum()),
        "Dropped / excluded": len(dropped_enrollments),
        "Unmatched attendance rows": len(unmatched_attendance),
        "Match method": match_method,
    }

    job_id = uuid.uuid4().hex[:12]
    output_dir = output_root / job_id
    output_dir.mkdir(parents=True, exist_ok=True)

    csv_path = output_dir / "attendance_follow_up.csv"
    follow_up[["No.", "Name", "Status", "Minutes Attended"]].to_csv(
        csv_path, index=False
    )

    xlsx_path = output_dir / "attendance_results.xlsx"
    write_excel_result(
        xlsx_path,
        follow_up,
        dropped_enrollments,
        all_enrollments,
        unmatched_attendance,
        summary,
    )

    return AttendanceResult(
        job_id=job_id,
        follow_up=follow_up,
        dropped_enrollments=dropped_enrollments,
        all_enrollments=all_enrollments,
        unmatched_attendance=unmatched_attendance,
        summary=summary,
        csv_path=csv_path,
        xlsx_path=xlsx_path,
    )


def read_table(file_bytes: bytes, filename: str, label: str) -> pd.DataFrame:
    if not file_bytes:
        raise AttendanceToolError(f"Please upload the {label} file.")

    suffix = Path(filename or "").suffix.lower()
    stream = io.BytesIO(file_bytes)

    try:
        if suffix in [".csv", ".txt"]:
            raw = pd.read_csv(stream, header=None)
            return normalize_loaded_table(raw, label)
        if suffix == ".tsv":
            raw = pd.read_csv(stream, sep="\t", header=None)
            return normalize_loaded_table(raw, label)
        if suffix in [".xlsx", ".xlsm", ".xltx", ".xltm", ""]:
            raw = pd.read_excel(stream, sheet_name=0, header=None, engine="openpyxl")
            return normalize_loaded_table(raw, label)
        if suffix == ".xls":
            raw = pd.read_excel(stream, sheet_name=0, header=None)
            return normalize_loaded_table(raw, label)
    except Exception as exc:
        raise AttendanceToolError(
            f"I could not read the {label} file. Please check that it is a valid Excel or CSV file. Details: {exc}"
        ) from exc

    raise AttendanceToolError(
        f"Unsupported {label} file type '{suffix}'. Please upload .xlsx, .csv, or .tsv."
    )


def normalize_loaded_table(raw: pd.DataFrame, label: str) -> pd.DataFrame:
    raw = raw.dropna(how="all").dropna(axis=1, how="all").reset_index(drop=True)
    if raw.empty:
        return raw

    header_row = detect_header_row(raw)
    headers = make_unique_headers(raw.iloc[header_row].tolist())
    data = raw.iloc[header_row + 1 :].copy()
    data.columns = headers
    data = trim_table_data(data)

    if data.empty:
        raise AttendanceToolError(
            f"I found headers in the {label} file, but no data rows after them."
        )

    return data.reset_index(drop=True)


def detect_header_row(raw: pd.DataFrame) -> int:
    alias_keys = [normalize_header(alias) for alias in HEADER_ALIASES]
    best_index = 0
    best_score = -1

    for index, row in raw.head(60).iterrows():
        values = [normalize_header(value) for value in row.tolist()]
        values = [value for value in values if value]
        score = 0
        for value in values:
            if value in alias_keys:
                score += 3
            elif any(alias and alias not in {"name", "email"} and alias in value for alias in alias_keys):
                score += 1
        if score > best_score:
            best_index = index
            best_score = score

    return int(best_index)


def make_unique_headers(values: Iterable[object]) -> list[str]:
    headers: list[str] = []
    seen: dict[str, int] = {}

    for position, value in enumerate(values, start=1):
        header = clean_display_value(value) or f"Column {position}"
        count = seen.get(header, 0) + 1
        seen[header] = count
        if count > 1:
            header = f"{header} {count}"
        headers.append(header)

    return headers


def trim_table_data(data: pd.DataFrame) -> pd.DataFrame:
    kept_indexes: list[int] = []
    seen_data = False

    for index, row in data.iterrows():
        values = [clean_display_value(value) for value in row.tolist()]
        is_blank = not any(values)
        first_value = values[0] if values else ""

        if is_blank:
            if seen_data:
                break
            continue

        if seen_data and re.match(r"^\d+\.\s+", first_value):
            break

        seen_data = True
        kept_indexes.append(index)

    if not kept_indexes:
        return data.iloc[0:0].copy()

    return data.loc[kept_indexes].dropna(how="all")


def get_display_name_series(df: pd.DataFrame, explicit_column: str = "") -> pd.Series:
    if explicit_column.strip():
        column = choose_existing_column(df, explicit_column, "enrollment name")
        return df[column].map(extract_person_name)

    enrollment_column = detect_exact_column(df, ENROLLMENT_FIELD_ALIASES)
    if enrollment_column:
        return df[enrollment_column].map(extract_person_name)

    name_column = detect_column(df, NAME_ALIASES)
    if name_column:
        split_name_series = get_split_teams_name_series(df, name_column)
        if split_name_series is not None:
            return split_name_series
        return df[name_column].map(extract_person_name)

    first_column = detect_column(df, FIRST_NAME_ALIASES)
    last_column = detect_column(df, LAST_NAME_ALIASES)
    if first_column and last_column:
        return (
            df[first_column].map(clean_display_value)
            + " "
            + df[last_column].map(clean_display_value)
        ).map(lambda value: re.sub(r"\s+", " ", value).strip())

    email_column = detect_column(df, EMAIL_ALIASES)
    if email_column:
        return df[email_column].map(clean_display_value)

    raise AttendanceToolError(
        "I could not find a name column in the enrollment sheet. "
        f"Available columns: {format_columns(df.columns)}"
    )


def get_name_source_series(
    df: pd.DataFrame, explicit_column: str = "", fallback: pd.Series | None = None
) -> pd.Series:
    if explicit_column.strip():
        column = choose_existing_column(df, explicit_column, "enrollment name")
        return df[column].map(clean_display_value)

    enrollment_column = detect_exact_column(df, ENROLLMENT_FIELD_ALIASES)
    if enrollment_column:
        return df[enrollment_column].map(clean_display_value)

    name_column = detect_column(df, NAME_ALIASES)
    if name_column:
        return df[name_column].map(clean_display_value)

    first_column = detect_column(df, FIRST_NAME_ALIASES)
    last_column = detect_column(df, LAST_NAME_ALIASES)
    if first_column and last_column:
        return (
            df[first_column].map(clean_display_value)
            + " "
            + df[last_column].map(clean_display_value)
        ).map(lambda value: re.sub(r"\s+", " ", value).strip())

    if fallback is not None:
        return fallback.map(clean_display_value)

    return pd.Series([""] * len(df), index=df.index)


def get_optional_name_series(df: pd.DataFrame) -> pd.Series:
    try:
        return get_display_name_series(df)
    except AttendanceToolError:
        return pd.Series([""] * len(df), index=df.index)


def get_match_series(
    enrollments: pd.DataFrame,
    attendance: pd.DataFrame,
    enrollment_match_column: str,
    attendance_match_column: str,
    enrollment_name: pd.Series,
) -> tuple[pd.Series, pd.Series, str, str]:
    if enrollment_match_column.strip() or attendance_match_column.strip():
        if not enrollment_match_column.strip() or not attendance_match_column.strip():
            raise AttendanceToolError(
                "If you enter a custom match column, please enter it for both sheets."
            )
        enrollment_column = choose_existing_column(
            enrollments, enrollment_match_column, "enrollment match"
        )
        attendance_column = choose_existing_column(
            attendance, attendance_match_column, "attendance match"
        )
        return (
            enrollments[enrollment_column],
            attendance[attendance_column],
            f"custom columns: {enrollment_column} / {attendance_column}",
            "generic",
        )

    enrollment_email = detect_column(enrollments, EMAIL_ALIASES)
    attendance_email = detect_column(attendance, EMAIL_ALIASES)
    if enrollment_email and attendance_email:
        return (
            enrollments[enrollment_email],
            attendance[attendance_email],
            f"email: {enrollment_email} / {attendance_email}",
            "email",
        )

    attendance_name = get_display_name_series(attendance)
    return (enrollment_name, attendance_name, "name (normalized)", "name")


def choose_column(
    df: pd.DataFrame, explicit_column: str, aliases: Iterable[str], label: str
) -> str:
    if explicit_column.strip():
        return choose_existing_column(df, explicit_column, label)

    detected = detect_column(df, aliases)
    if detected:
        return detected

    raise AttendanceToolError(
        f"I could not find the {label} column. Available columns: {format_columns(df.columns)}"
    )


def choose_existing_column(df: pd.DataFrame, requested: str, label: str) -> str:
    normalized_requested = normalize_header(requested)
    for column in df.columns:
        if normalize_header(column) == normalized_requested:
            return str(column)

    raise AttendanceToolError(
        f"I could not find the {label} column '{requested}'. "
        f"Available columns: {format_columns(df.columns)}"
    )


def detect_column(df: pd.DataFrame, aliases: Iterable[str]) -> str | None:
    alias_keys = [normalize_header(alias) for alias in aliases]
    normalized_columns = [(str(column), normalize_header(column)) for column in df.columns]

    for column, normalized in normalized_columns:
        if normalized in alias_keys:
            return column

    for column, normalized in normalized_columns:
        if any(alias and alias in normalized for alias in alias_keys):
            return column

    return None


def detect_exact_column(df: pd.DataFrame, aliases: Iterable[str]) -> str | None:
    alias_keys = [normalize_header(alias) for alias in aliases]
    for column in df.columns:
        if normalize_header(column) in alias_keys:
            return str(column)
    return None


def normalize_header(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value).strip().lower())


def normalize_match_key(value: object, key_mode: str = "generic") -> str:
    if key_mode == "name":
        return normalize_name_key(value)
    text = clean_display_value(value).lower()
    return re.sub(r"\s+", " ", text).strip()


def normalize_alias_key(value: object, key_mode: str = "generic") -> str:
    if key_mode != "name":
        return normalize_match_key(value, key_mode)
    return normalize_name_alias_key(value)


def normalize_name_key(value: object) -> str:
    tokens = normalize_name_tokens(value)
    return " ".join(sorted(tokens))


def normalize_name_alias_key(value: object) -> str:
    tokens = normalize_name_tokens(value)
    if len(tokens) < 2:
        return ""
    return " ".join([tokens[0], tokens[-1]])


def normalize_name_tokens(value: object) -> list[str]:
    text = reorder_comma_name(extract_person_name(value))
    text = remove_accents(text).lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    honorifics = {
        "mr",
        "mrs",
        "ms",
        "miss",
        "dr",
        "prof",
        "mx",
        "terminated",
        "dropped",
        "drop",
        "withdrawn",
        "withdraw",
        "cancelled",
        "canceled",
        "inactive",
    }
    return [token for token in text.split() if token and token not in honorifics]


def sort_by_name(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty or "Name" not in df.columns:
        return df
    return (
        df.assign(_sort_name=df["Name"].map(normalize_sort_key))
        .sort_values(["_sort_name", "Enrollment Sequence"], kind="mergesort")
        .drop(columns=["_sort_name"])
        .reset_index(drop=True)
    )


def add_row_numbers(df: pd.DataFrame) -> pd.DataFrame:
    numbered = df.drop(columns=["No."], errors="ignore").reset_index(drop=True).copy()
    numbered.insert(0, "No.", range(1, len(numbered) + 1))
    return numbered


def normalize_sort_key(value: object) -> str:
    text = remove_accents(clean_display_value(value)).lower()
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def get_split_teams_name_series(
    df: pd.DataFrame, name_column: str
) -> pd.Series | None:
    first_join_column = detect_exact_column(df, ["first join"])
    if not first_join_column:
        return None

    sample = df[first_join_column].dropna().head(20)
    if sample.empty:
        return None

    name_fragment_count = sum(
        bool(clean_display_value(value)) and not looks_like_time_or_date(value)
        for value in sample
    )
    time_like_count = sum(looks_like_time_or_date(value) for value in sample)
    if name_fragment_count <= time_like_count or name_fragment_count < 2:
        return None

    return df.apply(
        lambda row: combine_split_teams_name(
            row.get(name_column, ""), row.get(first_join_column, "")
        ),
        axis=1,
    )


def combine_split_teams_name(last_name: object, first_name: object) -> str:
    last = extract_person_name(last_name)
    first = extract_person_name(first_name)
    if first and last and not looks_like_time_or_date(first):
        return re.sub(r"\s+", " ", f"{first} {last}").strip()
    return last


def looks_like_time_or_date(value: object) -> bool:
    if pd.isna(value):
        return False
    if isinstance(value, (datetime, date, time, timedelta, pd.Timestamp)):
        return True
    text = clean_display_value(value).lower()
    return bool(
        re.search(r"\d{1,2}/\d{1,2}/\d{2,4}", text)
        or re.search(r"\d{4}-\d{1,2}-\d{1,2}", text)
        or re.search(r"\d{1,2}:\d{2}", text)
        or re.search(r"\b(am|pm)\b", text)
    )


def reorder_comma_name(value: object) -> str:
    text = clean_display_value(value)
    if "," not in text:
        return text
    left, right = [part.strip() for part in text.split(",", 1)]
    if not left or not right:
        return text
    return f"{right} {left}"


def remove_accents(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    return "".join(character for character in normalized if not unicodedata.combining(character))


def extract_person_name(value: object) -> str:
    text = clean_display_value(value)
    if " - " not in text:
        return strip_status_markers(text)

    first_part, _ = text.split(" - ", 1)
    if len(first_part.strip()) >= 2:
        return strip_status_markers(first_part.strip())
    return strip_status_markers(text)


def strip_status_markers(value: str) -> str:
    text = clean_display_value(value)
    return re.sub(
        r"\s*\((?:terminated|dropped|withdrawn|cancelled|canceled|inactive)\)\s*$",
        "",
        text,
        flags=re.IGNORECASE,
    ).strip()


def get_dropped_info(row: pd.Series, original_name: str) -> dict[str, object]:
    reasons: list[str] = []

    marker = extract_status_marker(original_name)
    if marker:
        reasons.append(marker)

    for column, value in row.items():
        header = normalize_header(column)
        text = clean_display_value(value)
        if not text:
            continue

        if header in {"dropdate", "dropreason"}:
            reasons.append(f"{column}: {format_cell_value(value)}")
        elif "status" in header and has_dropped_status(text):
            reasons.append(f"{column}: {text}")

    unique_reasons = list(dict.fromkeys(reasons))
    return {
        "is_dropped": bool(unique_reasons),
        "reason": "; ".join(unique_reasons),
    }


def extract_status_marker(value: object) -> str:
    text = clean_display_value(value)
    match = re.search(
        r"\((terminated|dropped|withdrawn|cancelled|canceled|inactive)\)\s*$",
        text,
        flags=re.IGNORECASE,
    )
    return match.group(1).capitalize() if match else ""


def has_dropped_status(value: object) -> bool:
    text = remove_accents(clean_display_value(value)).lower()
    return bool(
        re.search(r"\b(drop|dropped|withdraw|withdrawn|terminated|inactive)\b", text)
        or re.search(r"\bcancell?ed\b", text)
    )


def format_cell_value(value: object) -> str:
    if isinstance(value, (datetime, date, pd.Timestamp)):
        return value.strftime("%Y-%m-%d")
    return clean_display_value(value)


def build_unique_alias_lookup(attendance_summary: pd.DataFrame) -> dict[str, tuple[float, str]]:
    if "match_alias" not in attendance_summary:
        return {}

    grouped = attendance_summary.query("match_alias != ''").groupby("match_alias")
    return {
        alias: (float(group["minutes_attended"].sum()), clean_display_value(group["match_key"].iloc[0]))
        for alias, group in grouped
        if len(group) == 1
    }


def clean_display_value(value: object) -> str:
    if pd.isna(value):
        return ""
    return re.sub(r"\s+", " ", str(value).strip())


def parse_minutes(value: object) -> float:
    if pd.isna(value):
        return 0.0

    if isinstance(value, timedelta):
        return max(value.total_seconds() / 60, 0.0)

    if isinstance(value, time):
        return value.hour * 60 + value.minute + value.second / 60

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        if 0 < number <= 1:
            return max(number * 24 * 60, 0.0)
        return max(number, 0.0)

    text = str(value).strip().lower()
    if not text:
        return 0.0

    text = text.replace(",", "")

    if re.fullmatch(r"\d+(\.\d+)?", text):
        return max(float(text), 0.0)

    colon_match = re.fullmatch(r"(\d+):(\d{1,2})(?::(\d{1,2}))?", text)
    if colon_match:
        first = int(colon_match.group(1))
        second = int(colon_match.group(2))
        third = int(colon_match.group(3) or 0)
        if colon_match.group(3) is not None:
            return max(first * 60 + second + third / 60, 0.0)
        return max(first + second / 60, 0.0)

    hours = sum(
        float(match.group(1))
        for match in re.finditer(r"(\d+(?:\.\d+)?)\s*(?:h|hr|hrs|hour|hours)\b", text)
    )
    minutes = sum(
        float(match.group(1))
        for match in re.finditer(r"(\d+(?:\.\d+)?)\s*(?:m|min|mins|minute|minutes)\b", text)
    )
    seconds = sum(
        float(match.group(1))
        for match in re.finditer(r"(\d+(?:\.\d+)?)\s*(?:s|sec|secs|second|seconds)\b", text)
    )
    if hours or minutes or seconds:
        return max(hours * 60 + minutes + seconds / 60, 0.0)

    number_match = re.search(r"\d+(?:\.\d+)?", text)
    if number_match:
        return max(float(number_match.group(0)), 0.0)

    return 0.0


def classify_minutes(
    minutes: float, partial_threshold: float, attended_threshold: float
) -> str:
    if minutes >= attended_threshold:
        return "Attended"
    if minutes >= partial_threshold:
        return "Partially attended"
    return "Did not attend"


def first_non_blank(values: pd.Series) -> str:
    for value in values:
        cleaned = clean_display_value(value)
        if cleaned:
            return cleaned
    return ""


def format_columns(columns: Iterable[object]) -> str:
    return ", ".join(str(column) for column in columns)


def write_excel_result(
    xlsx_path: Path,
    follow_up: pd.DataFrame,
    dropped_enrollments: pd.DataFrame,
    all_enrollments: pd.DataFrame,
    unmatched_attendance: pd.DataFrame,
    summary: dict[str, int | str],
) -> None:
    summary_df = pd.DataFrame(
        [{"Metric": key, "Value": value} for key, value in summary.items()]
    )

    with pd.ExcelWriter(xlsx_path, engine="openpyxl") as writer:
        summary_df.to_excel(writer, sheet_name="Summary", index=False)
        follow_up[
            ["No.", "Name", "Status", "Minutes Attended", "Enrollment Sequence"]
        ].to_excel(
            writer, sheet_name="Needs Follow-up", index=False
        )
        dropped_enrollments[
            ["No.", "Name", "Dropped Reason", "Enrollment Sequence"]
        ].to_excel(writer, sheet_name="Dropped Enrollments", index=False)
        all_enrollments[
            [
                "No.",
                "Name",
                "Status",
                "Minutes Attended",
                "Enrollment Sequence",
                "Dropped Reason",
                "Match Method",
            ]
        ].to_excel(writer, sheet_name="All Enrollments", index=False)
        unmatched_attendance.to_excel(
            writer, sheet_name="Unmatched Attendance", index=False
        )

        for worksheet in writer.book.worksheets:
            worksheet.freeze_panes = "A2"
            for column_cells in worksheet.columns:
                max_length = max(
                    len(str(cell.value)) if cell.value is not None else 0
                    for cell in column_cells
                )
                adjusted_width = min(max(max_length + 2, 12), 48)
                worksheet.column_dimensions[column_cells[0].column_letter].width = (
                    adjusted_width
                )
