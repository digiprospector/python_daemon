# gui/config.py
from pathlib import Path

# --- 网络配置 ---
HOST = "127.0.0.1"  # 标准环回接口地址 (localhost)
PORT = 54321        # TCP服务器监听的端口
ENABLE_TCP_SERVER = True # 设置为 False 可以禁用TCP服务器

# --- 脚本配置 ---
CURRENT_DIR = Path(__file__).parent

# 定义由GUI管理的脚本。
# 每个条目都是一个字典，包含：
# - name: 选项卡和按钮的显示名称。
# - script: 要执行的python脚本的路径。
# - msg: 通过TCP套接字触发脚本的秘密消息。
SCRIPTS_CONFIG = [
    {
        "name": "测试脚本",
        "script": str(CURRENT_DIR / "test.py"),
        "msg": b"RUN_SCRIPT_TEST"
    }
    # 你可以在这里添加更多的脚本，例如：
]