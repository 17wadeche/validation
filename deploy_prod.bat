@echo off
setlocal

:: ====================================================================
:: Automated Deployment Script for a Python Flask Project on Windows
::
:: This script uses robocopy to securely transfer project files to a
:: remote server, while automatically excluding common development
:: artifacts like virtual environments, cache folders, and log files.
::
:: Instructions:
:: 1. Save this file as `deploy.bat` in the root directory of your project.
:: 2. Modify the `REMOTE_SERVER_PATH` variable below to match your server's
::    network path and destination folder.
:: 3. Double-click the file to run the deployment.
:: ====================================================================


:: ====================================================================
:: CONFIGURE THIS PATH
::
:: This is the most important part!
:: Update the path below to point to your project's destination on the
:: remote Windows server.
::
:: Example: set REMOTE_SERVER_PATH="\\MyServer\C$\inetpub\wwwroot\my-flask-app"
:: Make sure to include the double quotes if the path has spaces.
:: ====================================================================
set "REMOTE_SERVER_PATH=\\hcwda30449e\Validation-Tool

:: --- SCRIPT STARTS HERE ---

echo.
echo Starting deployment...

:: Check if the remote path has been configured
if "%REMOTE_SERVER_PATH%" == "" (
    echo [ERROR]: The REMOTE_SERVER_PATH variable has not been set.
    echo Please edit this script and configure the path to your server.
    goto end
)

:: Get the current directory (project root)
set "SOURCE_PATH=%~dp0"
:: Remove trailing backslash to avoid quote escaping issues
if "%SOURCE_PATH:~-1%"=="\" set "SOURCE_PATH=%SOURCE_PATH:~0,-1%"

echo.
echo Source: %SOURCE_PATH%
echo Destination: %REMOTE_SERVER_PATH%
echo.


:: Robocopy command to copy the project files
::
::  /E                :: Copy subdirectories, including empty ones.
::  /R:3              :: Retry 3 times on failed copies.
::  /W:5              :: Wait 5 seconds between retries.
::  /NP               :: No progress (cleaner output).
::  /NFL              :: No file list (less verbose).
::  /NDL              :: No directory list (less verbose).
::  /XF *~*           :: Exclude temporary files.
::  /XD venv          :: Exclude the virtual environment folder.
::  /XD __pycache__   :: Exclude Python cache folders.
::  /XD .vscode       :: Exclude VS Code configuration folder.
::  /XD logs          :: Exclude the project's logs folder.
::  /XD .git          :: Exclude the git repository folder.
::  /XD data          :: Exclude the data folder.

robocopy "%SOURCE_PATH%" "%REMOTE_SERVER_PATH%" /E /PURGE /R:3 /W:5 ^
/XF "*.tmp" "*.bat" "*~" "~*" ".gitignore" "Readme.md" "launcher.spec" "launcher.py" "validation-ui.spec" "credentials.json" "input.json" "inputs.json" ^
/XD ".venv" "__pycache__" ".vscode" "logs" ".git" "build" "dist" "installer" "packaging" "logs" "venv"

:: Check the exit code of robocopy to see if it was successful
if %errorlevel% leq 8 (
    echo.
    echo Deployment completed successfully!
) else (
    echo.
    echo [WARNING]: Deployment finished with errors. Please check the log above.
)

:end
echo.
pause
