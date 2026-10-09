@echo off
cd /d "%~dp0"
if not exist tools\apktool.jar python setup.py
python server.py %*
