@echo off
REM 本机盘后情绪自动刷新 —— 双击即跑(交互模式末尾暂停看结果);
REM 任务计划程序请填:  "C:\...\chanlun\sentiment\refresh_sentiment.bat" /silent
setlocal
set "SENT=%~dp0"
set "LOG=%SENT%refresh_sentiment.log"

REM 仅依赖 Python 3 标准库, 直接用 PATH 里的 python; 找不到则报错退出
where python >nul 2>nul
if errorlevel 1 (
  echo [%date% %time%] [错误] 未找到 python, 请先安装 Python 3 并加入 PATH >> "%LOG%"
  echo [错误] 未找到 python, 请先安装 Python 3 并加入 PATH
  if not "%~1"=="/silent" pause
  exit /b 1
)

echo [%date% %time%] === 情绪刷新开始 === >> "%LOG%"
if "%~1"=="/silent" (
  python "%SENT%refresh_sentiment.py" >> "%LOG%" 2>&1
  echo [%date% %time%] === 情绪刷新结束 (rc=%errorlevel%) === >> "%LOG%"
) else (
  python "%SENT%refresh_sentiment.py"
  echo.
  echo 已结束, 按任意键关闭此窗口...
  pause >nul
)
endlocal
