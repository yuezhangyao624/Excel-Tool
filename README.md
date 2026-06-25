# Attendance Checker

Attendance Checker is a small local web tool for comparing an enrollment export with an actual attendance export. It is designed for imperfect spreadsheet exports, including Microsoft Teams attendance reports and Learning Content enrollment exports.

The tool runs on your own computer. Uploaded spreadsheets are processed locally and are not sent to an external service.

## Features

- Upload an enrollment Excel or CSV file.
- Upload an actual attendance Excel or CSV file.
- Classify attendance by minutes:
  - `45` minutes or more: attended
  - `30` to less than `45` minutes: partially attended
  - less than `30` minutes: did not attend
- Show follow-up rows alphabetically.
- Move dropped, withdrawn, cancelled, inactive, or terminated no-shows into a separate dropped section.
- Handle Microsoft Teams exports with summary sections, activity sections, split name columns, and duration text like `1h 9m 48s`.
- Handle enrollment names like `Person Name - Course Name`.
- Download Excel and CSV results.

## Requirements

- Python 3.9 or newer
- The packages in `requirements.txt`

## Quick Start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python3 attendance_tool/app.py
```

Then open:

```text
http://127.0.0.1:8765
```

## Matching Logic

If both files include email columns, the tool matches by email. Otherwise, it matches by normalized name.

Name matching supports common export differences such as:

- `First Last`
- `Last, First`
- names with accents or punctuation
- enrollment fields like `First Last - Course Name`
- status markers like `(Terminated)`

If a person appears multiple times in the attendance file, their minutes are added together.

## Privacy

The app writes generated results to `attendance_tool/results/` on your computer. That folder is ignored by Git so personal attendance data is not committed accidentally.

## License

This project is licensed under the MIT License. See `LICENSE` for details.
