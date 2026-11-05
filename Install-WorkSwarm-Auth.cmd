@echo off
setlocal
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
chcp 65001 >nul
pushd "%~dp0"
if not exist "pyproject.toml" goto extract_all
if not exist "src\workswarm_auth\cli.py" goto extract_all
if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" goto known_python
py -3.12 -c "import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)" >nul 2>nul
if not errorlevel 1 goto py_launcher
python -c "import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)" >nul 2>nul
if errorlevel 1 goto missing_python
python windows_install.py %*
goto result
:known_python
"%LOCALAPPDATA%\Programs\Python\Python312\python.exe" windows_install.py %*
goto result
:py_launcher
py -3.12 windows_install.py %*
:result
set "INSTALL_EXIT=%ERRORLEVEL%"
goto finish
:extract_all
echo Extract All files from the ZIP before running this installer.
set "INSTALL_EXIT=1"
goto finish
:missing_python
echo Python 3.11+ is required. Install Python 3.12 and rerun.
set "INSTALL_EXIT=1"
:finish
echo Installation exit code: %INSTALL_EXIT%
pause
popd
exit /b %INSTALL_EXIT%
