import time
import sys
import argparse

def main():
    parser = argparse.ArgumentParser(description="一个接收参数的测试脚本。")
    parser.add_argument('args', nargs='*', help='任意数量的位置参数')
    parsed_args = parser.parse_args()

    print("测试脚本开始运行...")
    if parsed_args.args:
        print(f"收到的参数: {parsed_args.args}")
    else:
        print("未收到任何参数。")
    sys.stdout.flush()

    total_duration = 10  # 总运行时长（秒）
    interval = 1         # 输出间隔（秒）

    for i in range(total_duration):
        # 使用 `end='\r'` 来让光标回到行首，实现覆盖效果
        # 增加一些空格以确保完全覆盖上一行可能更长的内容
        print(f"已经运行 {i + 1} 秒...{' ' * 10}", end='\r')
        
        # 确保输出被立即发送，而不是被缓冲
        sys.stdout.flush()
        
        # 等待下一个间隔
        time.sleep(interval)
    
    # 循环结束后，打印一个换行符，以免下一条输出覆盖最后一条进度信息
    print()
    print("测试脚本运行结束。")

    sys.stdout.flush()

if __name__ == "__main__":
    main()
