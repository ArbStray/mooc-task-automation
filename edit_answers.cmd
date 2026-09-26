@echo off
setlocal
pushd "%~dp0"
if errorlevel 1 (
    echo Cannot open the script folder.
    pause
    exit /b 1
)
if not exist ".venv\Scripts\python.exe" (
    echo Python environment not found. Please run install.cmd first.
    popd
    pause
    exit /b 1
)
".venv\Scripts\python.exe" "answer_editor.py" %*
set "result=%errorlevel%"
popd
if not "%result%"=="0" pause
exit /b %result%
