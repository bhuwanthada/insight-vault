@echo off
cd /d "%~dp0"
where node >nul 2>nul || (echo Node.js 18+ is required: https://nodejs.org & pause & exit /b 1)
if not exist .venv\Scripts\python.exe (echo First run: run "npm run setup" once, then start again. & pause & exit /b 1)
node scripts\start.mjs
