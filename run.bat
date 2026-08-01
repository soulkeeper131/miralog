@echo off
cd /d C:\Users\vladi\Documents\Projects\Miralog
"C:\Users\vladi\Documents\Projects\Miralog\venv\Scripts\python.exe" -m uvicorn app:app --host 127.0.0.1 --port 8000
