@echo off
echo 啟動 Flask 伺服器...

REM 啟動虛擬環境
call venv\Scripts\activate

REM 設定 Flask 環境
set FLASK_APP=app.py
set FLASK_ENV=development

REM 自動開啟預設瀏覽器，導向 http://127.0.0.1:5000
start "" http://127.0.0.1:5000

REM 啟動 Flask
python -m flask run

pause
