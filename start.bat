@echo off
cd /d "%~dp0"
call venv\Scripts\activate
python nocturnal_server.py
