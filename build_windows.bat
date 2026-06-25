@echo off
setlocal

python -m pip install --upgrade pip
python -m pip install -r requirements-build.txt
python -m PyInstaller --clean --onefile --console --name AttendanceChecker attendance_tool\app.py

echo.
echo Build complete: dist\AttendanceChecker.exe
echo Double-click AttendanceChecker.exe to run the tool.

