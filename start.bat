@echo off
chcp 65001 >nul
cd /d %~dp0
echo ===== 安装依赖 =====
python -m pip install -q -r requirements.txt
echo ===== 启动服务 =====
python run.py
pause
