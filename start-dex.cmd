@echo off
cd /d "%~dp0"
"C:\Python312\python.exe" dex_sim.py --steps 100 --interval 0.1
pause
