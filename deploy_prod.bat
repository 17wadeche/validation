@echo off
setlocal
set "REMOTE_SERVER_PATH=\\hcwda30449e\Validation-Tool"
echo.
echo Starting deployment...
if "%REMOTE_SERVER_PATH%" == "" (
    echo [ERROR]: The REMOTE_SERVER_PATH variable has not been set.
    echo Please edit this script and configure the path to your server.
    goto end
)
set "SOURCE_PATH=%~dp0"
if "%SOURCE_PATH:~-1%"=="\" set "SOURCE_PATH=%SOURCE_PATH:~0,-1%"
echo.
echo Source: %SOURCE_PATH%
echo Destination: %REMOTE_SERVER_PATH%
echo.
robocopy "%SOURCE_PATH%" "%REMOTE_SERVER_PATH%" /E /PURGE /R:3 /W:5 ^
/XF "*.tmp" "*.bat" "*~" "~*" ".gitignore" "Readme.md" "launcher.spec" "launcher.py" "validation-ui.spec" ^
/XD ".venv" "__pycache__" ".vscode" "logs" ".git" "build" "dist" "installer" "packaging" "venv"
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
