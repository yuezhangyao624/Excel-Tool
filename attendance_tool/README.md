# Attendance Checker

A small local tool for comparing an enrollment sheet against an actual attendance sheet.

## What It Does

- Upload one enrollment file and one actual attendance file.
- Uses attended minutes to classify each enrolled person:
  - `45` minutes or more: attended
  - `30` to less than `45` minutes: partially attended
  - less than `30` minutes, or missing from attendance: did not attend
- Outputs the people who need follow-up in alphabetical order.
- Shows a fresh `No.` column for the alphabetized result; the downloaded Excel also keeps the original enrollment sequence for reference.
- Moves dropped, withdrawn, cancelled, inactive, or terminated enrollments into a separate section when no attendance is found for them.
- Downloads both an Excel workbook and a CSV file.

## Start The Tool

### Windows app

If you downloaded `AttendanceChecker.exe`, double-click it. The app opens your browser automatically. Keep the console window open while using the tool.

### Python

From the repository root:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python3 attendance_tool/app.py
```

Or, if the dependencies are already installed:

```bash
python3 attendance_tool/app.py
```

Then open:

```text
http://127.0.0.1:8765
```

## Expected Columns

The tool automatically looks for common columns such as `Name`, `Full Name`, `Email`, `Minutes Attended`, and `Duration`.

If your files use different names, open **Column settings** on the upload page and enter the exact column names.

## Matching

If both files have an email column, the tool matches by email. Otherwise it matches by name.

For Microsoft Teams attendance reports, it automatically skips the summary section and reads the participant table. For Learning Content enrollment exports where the `Enrollment` field looks like `Person Name - Course Name`, it uses only the person name for matching.

Names are normalized before matching, so formats like `First Last` and `Last, First` can still match. Accents and punctuation are also normalized.

If someone appears more than once in the actual attendance sheet, their minutes are added together.
