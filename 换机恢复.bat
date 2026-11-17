@echo off
chcp 65001 >nul
cd /d F:\微信监控\wx-assist
F:\微信监控\.venv\Scripts\python.exe -m src.journal.cloud.restore --apply --rebuild-index
pause
