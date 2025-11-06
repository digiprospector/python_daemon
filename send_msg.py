# send_msg.py
import socket
import argparse
import sys

# --- 配置 ---
from config import HOST, PORT

def send_trigger_message(message):
    """连接到监听程序并发送触发消息。"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.connect((HOST, PORT))
            s.sendall(message.encode('utf-8'))
            response = s.recv(1024)
            print(f"已发送消息 '{message}', 收到响应: {response.decode('utf-8').strip()}")
    except ConnectionRefusedError:
        print(f"错误: 连接被拒绝。守护程序(gui.py)是否正在端口 {PORT} 上运行?")
    except Exception as e:
        print(f"发生了一个错误: {e}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="向守护程序发送触发消息。",
        epilog="示例: python send_msg.py RUN_SCRIPT_1ST"
    )
    parser.add_argument("message", help="要发送的秘密消息 (例如: 'RUN_SCRIPT_1ST', 'RUN_SCRIPT_2ND')")
    args = parser.parse_args()

    send_trigger_message(args.message)