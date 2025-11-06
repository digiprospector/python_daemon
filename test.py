import time
import sys

def main():
    print("测试脚本开始运行...")
    sys.stdout.flush()

    total_duration = 5  # 总运行时长（秒）
    interval = 1         # 输出间隔（秒）

    for i in range(total_duration):
        # 打印带有进度的消息
        print(f"已经运行 {i + 1} 秒...")
        
        # 确保输出被立即发送，而不是被缓冲
        sys.stdout.flush()
        
        # 等待下一个间隔
        time.sleep(interval)

    for i in range(total_duration, 2*total_duration-1):
        # 打印带有进度的消息
        print(f"\r已经运行 {i + 1} 秒...", end='')
        
        # 确保输出被立即发送，而不是被缓冲
        sys.stdout.flush()
        
        # 等待下一个间隔
        time.sleep(interval)

    print(f"\r已经运行 {2*total_duration} 秒...")

    print("测试脚本运行结束。")

    sys.stdout.flush()

if __name__ == "__main__":
    main()
