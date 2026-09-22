@echo off
REM Ejecuta el bot una vez desde esta carpeta y guarda la salida (para el Programador de tareas de Windows).
cd /d "%~dp0"
if not exist data mkdir data
python bot.py >> data\ejecucion_local.log 2>&1
