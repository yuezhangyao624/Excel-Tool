from __future__ import annotations

import cgi
import html
import mimetypes
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from attendance_processor import AttendanceToolError, analyze_attendance_files


BASE_DIR = (
    Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parent
)
RESULTS_DIR = BASE_DIR / "results"
MAX_UPLOAD_BYTES = 50 * 1024 * 1024


class AttendanceHandler(BaseHTTPRequestHandler):
    server_version = "AttendanceTool/1.0"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self.send_html(render_form())
            return

        if parsed.path.startswith("/download/"):
            self.serve_download(parsed.path)
            return

        self.send_error(404, "Not found")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/analyze":
            self.send_error(404, "Not found")
            return

        try:
            self.handle_analyze()
        except AttendanceToolError as exc:
            self.send_html(render_form(error=str(exc)), status=400)
        except Exception as exc:  # Keep the page friendly if an unexpected file issue appears.
            self.send_html(
                render_form(error=f"Something went wrong while processing the files: {exc}"),
                status=500,
            )

    def handle_analyze(self) -> None:
        content_length = int(self.headers.get("Content-Length", "0"))
        if content_length > MAX_UPLOAD_BYTES:
            raise AttendanceToolError("The uploaded files are too large. Please keep them under 50 MB total.")

        form = cgi.FieldStorage(
            fp=self.rfile,
            headers=self.headers,
            environ={
                "REQUEST_METHOD": "POST",
                "CONTENT_TYPE": self.headers.get("Content-Type", ""),
            },
        )

        enrollment_file = get_file_field(form, "enrollment_file")
        attendance_file = get_file_field(form, "attendance_file")

        partial_threshold = parse_float_field(form, "partial_threshold", 30)
        attended_threshold = parse_float_field(form, "attended_threshold", 45)

        result = analyze_attendance_files(
            enrollment_bytes=enrollment_file["bytes"],
            enrollment_filename=enrollment_file["filename"],
            attendance_bytes=attendance_file["bytes"],
            attendance_filename=attendance_file["filename"],
            output_root=RESULTS_DIR,
            enrollment_name_column=get_text_field(form, "enrollment_name_column"),
            enrollment_match_column=get_text_field(form, "enrollment_match_column"),
            attendance_match_column=get_text_field(form, "attendance_match_column"),
            attendance_minutes_column=get_text_field(form, "attendance_minutes_column"),
            partial_threshold=partial_threshold,
            attended_threshold=attended_threshold,
        )
        self.send_html(render_result(result))

    def serve_download(self, path: str) -> None:
        parts = [unquote(part) for part in path.split("/") if part]
        if len(parts) != 3:
            self.send_error(404, "Not found")
            return

        _, job_id, filename = parts
        if not job_id.isalnum() or filename not in {
            "attendance_follow_up.csv",
            "attendance_results.xlsx",
        }:
            self.send_error(404, "Not found")
            return

        file_path = (RESULTS_DIR / job_id / filename).resolve()
        try:
            file_path.relative_to(RESULTS_DIR.resolve())
        except ValueError:
            self.send_error(404, "Not found")
            return

        if not file_path.exists():
            self.send_error(404, "Not found")
            return

        content_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header(
            "Content-Disposition", f'attachment; filename="{file_path.name}"'
        )
        self.send_header("Content-Length", str(file_path.stat().st_size))
        self.end_headers()
        with file_path.open("rb") as file:
            self.wfile.write(file.read())

    def log_message(self, format: str, *args: object) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), format % args))

    def send_html(self, body: str, status: int = 200) -> None:
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def get_file_field(form: cgi.FieldStorage, name: str) -> dict[str, bytes | str]:
    field = form[name] if name in form else None
    if field is None or not getattr(field, "filename", ""):
        raise AttendanceToolError("Please upload both Excel files.")

    data = field.file.read()
    return {"filename": field.filename, "bytes": data}


def get_text_field(form: cgi.FieldStorage, name: str) -> str:
    value = form.getfirst(name, "")
    return str(value).strip()


def parse_float_field(form: cgi.FieldStorage, name: str, default: float) -> float:
    value = get_text_field(form, name)
    if not value:
        return default
    try:
        return float(value)
    except ValueError as exc:
        raise AttendanceToolError(f"Please enter a valid number for {name.replace('_', ' ')}.") from exc


def render_form(error: str = "") -> str:
    error_html = (
        f'<div class="alert">{html.escape(error)}</div>'
        if error
        else ""
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Attendance Checker</title>
  {styles()}
</head>
<body>
  <main class="shell">
    <header class="topbar">
      <div>
        <h1>Attendance Checker</h1>
        <p>Compare enrollment and actual attendance files.</p>
      </div>
    </header>

    {error_html}

    <form class="panel" method="post" action="/analyze" enctype="multipart/form-data">
      <section class="grid two">
        <label class="field">
          <span>Enrollment sheet</span>
          <input type="file" name="enrollment_file" accept=".xlsx,.xls,.csv,.tsv" required>
        </label>
        <label class="field">
          <span>Actual attendance sheet</span>
          <input type="file" name="attendance_file" accept=".xlsx,.xls,.csv,.tsv" required>
        </label>
      </section>

      <section class="thresholds">
        <label class="field compact">
          <span>Partial from minutes</span>
          <input type="number" name="partial_threshold" value="30" min="0" step="0.1">
        </label>
        <label class="field compact">
          <span>Attended from minutes</span>
          <input type="number" name="attended_threshold" value="45" min="0" step="0.1">
        </label>
      </section>

      <details class="advanced">
        <summary>Column settings</summary>
        <section class="grid two inner">
          <label class="field">
            <span>Enrollment name column</span>
            <input type="text" name="enrollment_name_column" placeholder="Name">
          </label>
          <label class="field">
            <span>Attendance minutes column</span>
            <input type="text" name="attendance_minutes_column" placeholder="Minutes Attended">
          </label>
          <label class="field">
            <span>Enrollment match column</span>
            <input type="text" name="enrollment_match_column" placeholder="Email">
          </label>
          <label class="field">
            <span>Attendance match column</span>
            <input type="text" name="attendance_match_column" placeholder="Email">
          </label>
        </section>
      </details>

      <button class="primary" type="submit">Process files</button>
    </form>
  </main>
</body>
</html>"""


def render_result(result) -> str:
    summary_items = "".join(
        f"<li><strong>{html.escape(str(value))}</strong><span>{html.escape(str(key))}</span></li>"
        for key, value in result.summary.items()
        if key != "Match method"
    )

    table_html = render_table(
        result.follow_up[["No.", "Name", "Status", "Minutes Attended"]]
    )
    if result.follow_up.empty:
        table_html = '<p class="empty">Everyone in the enrollment sheet attended at least the required number of minutes.</p>'

    unmatched_html = ""
    if not result.unmatched_attendance.empty:
        unmatched_html = f"""
        <details class="advanced">
          <summary>Unmatched attendance records</summary>
          <div class="table-wrap">{render_table(result.unmatched_attendance)}</div>
        </details>
        """

    dropped_html = ""
    if not result.dropped_enrollments.empty:
        dropped_html = f"""
        <section class="panel section-gap">
          <div class="section-heading">
            <h2>Dropped enrollments</h2>
            <span>Excluded when no attendance was found</span>
          </div>
          <div class="table-wrap">{render_table(result.dropped_enrollments[["No.", "Name", "Dropped Reason"]])}</div>
        </section>
        """

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Attendance Results</title>
  {styles()}
</head>
<body>
  <main class="shell">
    <header class="topbar">
      <div>
        <h1>Attendance Results</h1>
        <p>Sorted alphabetically by name.</p>
      </div>
      <a class="secondary" href="/">New check</a>
    </header>

    <ul class="metrics">{summary_items}</ul>

    <section class="actions">
      <a class="primary link" href="/download/{result.job_id}/attendance_results.xlsx">Download Excel</a>
      <a class="secondary" href="/download/{result.job_id}/attendance_follow_up.csv">Download CSV</a>
    </section>

    <section class="panel">
      <div class="section-heading">
        <h2>Needs follow-up</h2>
        <span>Matched by {html.escape(str(result.summary["Match method"]))}</span>
      </div>
      <div class="table-wrap">{table_html}</div>
    </section>

    {dropped_html}
    {unmatched_html}
  </main>
</body>
</html>"""


def render_table(df) -> str:
    if df.empty:
        return '<p class="empty">No rows to show.</p>'
    headers = "".join(f"<th>{html.escape(str(column))}</th>" for column in df.columns)
    rows = []
    for _, row in df.iterrows():
        cells = "".join(f"<td>{html.escape(str(value))}</td>" for value in row)
        rows.append(f"<tr>{cells}</tr>")
    return f"<table><thead><tr>{headers}</tr></thead><tbody>{''.join(rows)}</tbody></table>"


def styles() -> str:
    return """<style>
:root {
  color-scheme: light;
  --bg: #f7f5ef;
  --ink: #1f2a2e;
  --muted: #667278;
  --line: #d9d4c8;
  --panel: #ffffff;
  --accent: #1d6f5f;
  --accent-dark: #145247;
  --alert: #b3261e;
  --soft: #e9f1ee;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  min-height: 100vh;
  background: var(--bg);
  color: var(--ink);
  font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}
.shell {
  width: min(1080px, calc(100% - 32px));
  margin: 0 auto;
  padding: 32px 0 48px;
}
.topbar {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 24px;
  margin-bottom: 24px;
}
h1, h2, p { margin: 0; }
h1 {
  font-size: clamp(30px, 4vw, 48px);
  line-height: 1.05;
  letter-spacing: 0;
}
h2 {
  font-size: 20px;
  letter-spacing: 0;
}
.topbar p, .section-heading span {
  color: var(--muted);
  margin-top: 8px;
}
.panel {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 22px;
  box-shadow: 0 18px 35px rgba(31, 42, 46, 0.06);
}
.section-gap { margin-top: 20px; }
.grid {
  display: grid;
  gap: 18px;
}
.two {
  grid-template-columns: repeat(2, minmax(0, 1fr));
}
.field {
  display: grid;
  gap: 8px;
  color: var(--muted);
  font-size: 14px;
}
.field span {
  font-weight: 650;
  color: var(--ink);
}
input[type="file"], input[type="number"], input[type="text"] {
  width: 100%;
  min-height: 44px;
  border: 1px solid var(--line);
  border-radius: 6px;
  padding: 10px 12px;
  background: #fff;
  color: var(--ink);
  font: inherit;
}
input[type="file"] { padding: 9px; }
.thresholds {
  display: flex;
  gap: 16px;
  flex-wrap: wrap;
  margin-top: 20px;
}
.compact { width: min(220px, 100%); }
.advanced {
  margin: 20px 0;
  border-top: 1px solid var(--line);
  border-bottom: 1px solid var(--line);
  padding: 14px 0;
}
.advanced summary {
  cursor: pointer;
  font-weight: 700;
  color: var(--accent-dark);
}
.inner { margin-top: 16px; }
.primary, .secondary {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-height: 44px;
  border-radius: 6px;
  padding: 0 18px;
  border: 1px solid transparent;
  font-weight: 750;
  text-decoration: none;
  font: inherit;
}
.primary {
  background: var(--accent);
  color: #fff;
  cursor: pointer;
}
.primary:hover { background: var(--accent-dark); }
.secondary {
  color: var(--accent-dark);
  background: var(--soft);
  border-color: #bfd7ce;
}
.alert {
  margin-bottom: 18px;
  border: 1px solid rgba(179, 38, 30, 0.28);
  color: var(--alert);
  background: #fff8f6;
  border-radius: 8px;
  padding: 14px 16px;
  font-weight: 650;
}
.metrics {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: 12px;
  padding: 0;
  margin: 0 0 20px;
  list-style: none;
}
.metrics li {
  background: #fff;
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 16px;
}
.metrics strong {
  display: block;
  font-size: 28px;
  line-height: 1;
}
.metrics span {
  display: block;
  color: var(--muted);
  margin-top: 8px;
  font-size: 13px;
}
.actions {
  display: flex;
  gap: 12px;
  margin-bottom: 20px;
  flex-wrap: wrap;
}
.link { text-decoration: none; }
.section-heading {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  gap: 18px;
  margin-bottom: 16px;
}
.table-wrap {
  overflow-x: auto;
}
table {
  width: 100%;
  border-collapse: collapse;
  background: #fff;
}
th, td {
  padding: 12px 10px;
  border-bottom: 1px solid var(--line);
  text-align: left;
  white-space: nowrap;
}
th {
  font-size: 13px;
  color: var(--muted);
  background: #faf9f5;
}
.empty {
  color: var(--muted);
  padding: 10px 0;
}
@media (max-width: 760px) {
  .shell { width: min(100% - 24px, 1080px); padding-top: 20px; }
  .topbar { display: block; }
  .topbar .secondary { margin-top: 16px; }
  .two, .metrics { grid-template-columns: 1fr; }
  .panel { padding: 16px; }
  .section-heading { display: block; }
  th, td { white-space: normal; }
}
</style>"""


def create_server(host: str, port: int) -> tuple[ThreadingHTTPServer, int]:
    last_error: OSError | None = None
    for candidate_port in range(port, port + 25):
        try:
            return ThreadingHTTPServer((host, candidate_port), AttendanceHandler), candidate_port
        except OSError as exc:
            last_error = exc

    if last_error is not None:
        raise last_error
    raise OSError("Could not start the local server.")


def open_browser(url: str) -> None:
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()


def run(host: str = "127.0.0.1", port: int = 8765, launch_browser: bool = False) -> None:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    server, selected_port = create_server(host, port)
    url = f"http://{host}:{selected_port}"
    print(f"Attendance Checker running at {url}")
    print("Keep this window open while using the tool.")
    if launch_browser:
        open_browser(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nAttendance Checker stopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    selected_port = 8765
    launch_browser = getattr(sys, "frozen", False) or "--open-browser" in sys.argv
    numeric_args = [arg for arg in sys.argv[1:] if arg.isdigit()]
    if numeric_args:
        selected_port = int(numeric_args[0])
    run(port=selected_port, launch_browser=launch_browser)
