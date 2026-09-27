@echo off
chcp 65001 >nul
cd /d "%~dp0"
python -c "import playwright" 2>nul || python -m pip install -r requirements.txt
python chaoxing_auto_course.py --channel msedge %*
echo.
pause
