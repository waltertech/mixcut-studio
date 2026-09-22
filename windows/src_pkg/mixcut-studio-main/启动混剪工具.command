#!/bin/bash
cd "$(dirname "$0")" || exit 1
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
python3 launch.py
if [ "$?" -ne 0 ]; then
  read -r -p "按回车关闭窗口…"
fi
