# 守护程序主应用
import sys
from pathlib import Path
import shutil
 
from PySide6.QtCore import QObject, Signal, Slot, QProcess, QTimer, QProcessEnvironment, QEvent, Qt, QSettings
from PySide6.QtNetwork import QTcpServer, QHostAddress
from PySide6.QtWidgets import QApplication, QMainWindow, QSystemTrayIcon, QMenu, QMessageBox, QStyle, QTextEdit, QVBoxLayout, QWidget, QFontDialog, QTabWidget, QPushButton, QHBoxLayout
from PySide6.QtGui import QIcon, QAction, QTextCursor, QFont, QPalette

# --- 配置文件检查 ---
# 在导入配置之前，检查config.py是否存在。如果不存在，则从config_sample.py复制。
CURRENT_SCRIPT_DIR = Path(__file__).parent
config_path = CURRENT_SCRIPT_DIR / "config.py"
sample_config_path = CURRENT_SCRIPT_DIR / "config_sample.py"

if not config_path.exists():
    print(f"'config.py' 未找到。正在尝试从 '{sample_config_path.name}' 创建...")
    if sample_config_path.exists():
        try:
            shutil.copy(sample_config_path, config_path)
            print(f"成功创建 'config.py'。请根据您的需求编辑该文件后重新启动程序。")
        except Exception as e:
            print(f"错误: 从模板创建 'config.py' 失败: {e}")
            sys.exit(1) # 关键文件创建失败，退出程序
    else:
        print(f"错误: '{config_path.name}' 和 '{sample_config_path.name}' 都不存在。程序无法启动。")
        sys.exit(1) # 缺少关键文件，退出程序

# --- 配置 ---
from config import SCRIPTS_CONFIG, HOST, PORT, ENABLE_TCP_SERVER

PYTHON_EXECUTABLE = sys.executable # 使用运行此脚本的同一个Python解释器
# ---------------------

class ScriptRunner(QObject):
    """处理外部脚本的运行，允许多个脚本并发执行。"""
    setup_error = Signal(str, str)      # 脚本ID, 消息
    log_message = Signal(str, str)      # 脚本ID, 消息
    started_message = Signal(str)       # 脚本ID
    finished_message = Signal(str, str) # 脚本ID, 消息

    def __init__(self, parent=None):
        super().__init__(parent)
        self.processes = {}  # 脚本ID -> {process: QProcess, name: str}

    @Slot(str)
    def run_script(self, script_path_str, args=None):
        """以非阻塞方式运行目标python脚本。"""
        script_path = Path(script_path_str)
        script_id = str(script_path.absolute())

        # 如果脚本已在运行，则终止它
        if script_id in self.processes and self.processes[script_id]['process'].state() != QProcess.ProcessState.NotRunning:
            self.stop_script(script_id) # 注意：这里是停止脚本，不是重启。如果需要带新参数重启，逻辑会更复杂。
            return True # 返回True表示执行了操作

        if not script_path.exists():
            error_msg = f"错误: 脚本 '{script_path_str}' 未找到。"
            print(error_msg)
            self.setup_error.emit(script_id, error_msg)
            return False

        arguments = [script_id]
        if args:
            arguments.extend(args)

        print(f"开始运行脚本: {PYTHON_EXECUTABLE} {' '.join(arguments)}")
        self.log_message.emit(script_id, f"--- 开始运行脚本: {script_path.name} ---\n")
        self.started_message.emit(script_id)
        
        process = QProcess()
        self.processes[script_id] = {'process': process, 'name': script_path.name}

        # 设置子进程的环境变量，强制其输出为UTF-8，解决中文乱码问题
        env = QProcessEnvironment.systemEnvironment()
        env.insert("PYTHONIOENCODING", "utf-8")
        process.setProcessEnvironment(env)

        # 连接信号和槽
        process.readyReadStandardOutput.connect(lambda: self.handle_stdout(script_id))
        process.readyReadStandardError.connect(lambda: self.handle_stderr(script_id))
        process.finished.connect(lambda code, status: self.on_finished(script_id, code, status))
        process.start(PYTHON_EXECUTABLE, arguments)

        print(f"'{script_path_str}' 已启动。")
        return True

    @Slot(str)
    def stop_script(self, script_id):
        """通过脚本ID停止正在运行的脚本。"""
        if script_id in self.processes and self.processes[script_id]['process'].state() != QProcess.ProcessState.NotRunning:
            self.log_message.emit(script_id, f"--- 正在终止脚本: {self.processes[script_id]['name']} ---\n")
            self.processes[script_id]['process'].kill()
            return True
        return False

    def handle_stdout(self, script_id):
        if script_id in self.processes:
            process = self.processes[script_id]['process']
            data = process.readAllStandardOutput().data().decode('utf-8', errors='ignore')
            self.log_message.emit(script_id, data)

    def handle_stderr(self, script_id):
        if script_id in self.processes:
            process = self.processes[script_id]['process']
            data = process.readAllStandardError().data().decode('utf-8', errors='ignore')
            self.log_message.emit(script_id, data)

    def on_finished(self, script_id, exit_code, exit_status):
        status_text = "正常退出" if exit_status == QProcess.ExitStatus.NormalExit else "崩溃"
        script_name = self.processes[script_id]['name']
        self.log_message.emit(script_id, f"\n--- 脚本运行结束 (退出码: {exit_code}, 状态: {status_text}) ---\n")
        self.finished_message.emit(script_id, f"{script_name} 脚本运行结束 (退出码: {exit_code}, 状态: {status_text})")
        if script_id in self.processes:
            del self.processes[script_id]


class Server(QObject):
    """一个简单的TCP服务器，用于监听特定消息。"""
    trigger_script = Signal(str, list) # 触发信号，参数为脚本路径和参数列表

    def __init__(self, parent=None):
        super().__init__(parent)
        self._server = QTcpServer(self)
        self._server.newConnection.connect(self.on_new_connection)
        self.message_map = {config['msg'].decode('utf-8'): {'script': config['script'], 'args': config.get('args', [])} for config in SCRIPTS_CONFIG}

    def start(self):
        if not self._server.listen(QHostAddress(HOST), PORT):
            print(f"错误: 无法在端口 {PORT} 上启动服务器。")
            return False
        print(f"正在监听 {self._server.serverAddress().toString()}:{self._server.serverPort()}...")
        return True

    def stop(self):
        self._server.close()
        print("服务器已停止。")

    @Slot()
    def on_new_connection(self):
        socket = self._server.nextPendingConnection()
        if socket:
            socket.readyRead.connect(lambda: self.on_ready_read(socket))
            socket.disconnected.connect(socket.deleteLater)
            print("客户端已连接。")

    def on_ready_read(self, socket):
        data = socket.readAll().data().decode('utf-8').strip()
        print(f"收到数据: {data}")
        if data in self.message_map: # 比较字符串
            script_info = self.message_map[data]
            script_to_run = script_info['script']
            args_to_run = script_info['args']
            print(f"收到消息 '{data}'! 正在触发脚本: {script_to_run} 带参数: {args_to_run}")
            self.trigger_script.emit(script_to_run, args_to_run)
            socket.write(f"确认: 已触发 {Path(script_to_run).name}。\n".encode('utf-8'))
        else:
            socket.write("错误: 无效消息。\n".encode('utf-8'))
        socket.disconnectFromHost()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("后台脚本守护程序")
        self.setGeometry(100, 100, 800, 600)

        # --- 设置 ---
        self.settings = QSettings("my_company", "daemon_gui")

        # --- 主窗口和布局 ---
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        layout = QVBoxLayout(central_widget)

        # --- 用于管理多个脚本的Tab窗口 ---
        self.tab_widget = QTabWidget()
        layout.addWidget(self.tab_widget)

        self.tabs_info = {} # 脚本ID -> {log_display, button, index}

        for config in SCRIPTS_CONFIG:
            script_path = config['script']
            script_id = str(Path(script_path).absolute())
            tab_name = config['name']
            args = config.get('args', []) # 获取参数，如果不存在则为空列表

            tab = QWidget()
            tab_layout = QVBoxLayout(tab)

            run_button = QPushButton(f"运行 {tab_name}")
            run_button.clicked.connect(lambda _, s=script_id: self.toggle_script(s))

            log_display = QTextEdit()
            log_display.setReadOnly(True)
            log_display.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
            log_display.setStyleSheet("background-color: #F5F5DC;") # 设置米色背景

            tab_layout.addWidget(run_button)
            tab_layout.addWidget(log_display)

            index = self.tab_widget.addTab(tab, tab_name)
            self.tabs_info[script_id] = {"log_display": log_display, "button": run_button, "index": index, "path": script_path, "args": args}

        # --- UI创建后加载设置 ---
        self.load_settings()

        # --- 设置菜单栏 ---
        menu_bar = self.menuBar()
        settings_menu = menu_bar.addMenu("设置")

        en_font_action = QAction("英文字体...", self)
        en_font_action.triggered.connect(lambda: self.select_font('en'))
        settings_menu.addAction(en_font_action)

        zh_font_action = QAction("中文字体...", self)
        zh_font_action.triggered.connect(lambda: self.select_font('zh'))
        settings_menu.addAction(zh_font_action)

        self.apply_fonts()

        # --- 系统托盘图标 ---
        self.tray_icon = QSystemTrayIcon(self)
        # 程序会使用同目录下的 icon.png 文件作为图标
        icon_path = CURRENT_SCRIPT_DIR / "icon.png" 
        app_icon = None
        if icon_path.exists():
            app_icon = QIcon(str(icon_path))
        else:
            # 如果找不到图标，则使用标准图标
            app_icon = QApplication.style().standardIcon(QStyle.SP_ComputerIcon)

        self.setWindowIcon(app_icon)
        self.tray_icon.setIcon(app_icon)

        tray_menu = QMenu()
        show_action = QAction("显示", self)
        show_action.triggered.connect(self.show)
        
        quit_action = QAction("退出", self)
        quit_action.triggered.connect(self.quit_application)

        tray_menu.addAction(show_action)
        tray_menu.addAction(quit_action)
        self.tray_icon.setContextMenu(tray_menu)
        self.tray_icon.show()
        self.tray_icon.setToolTip("守护程序正在运行。")
        self.tray_icon.activated.connect(self.on_tray_icon_activated)

        # --- 核心逻辑 ---
        self.server = None
        self.runner = ScriptRunner()
        self.runner.setup_error.connect(self.show_error_message)
        self.runner.started_message.connect(self.mark_tab_as_running)
        self.runner.log_message.connect(self.append_log_message)
        self.runner.finished_message.connect(self.handle_script_finished)

        if ENABLE_TCP_SERVER:
            self.server = Server()
            self.server.trigger_script.connect(self.runner.run_script)
            if not self.server.start():
                QMessageBox.critical(self, "服务器错误", f"无法在端口 {PORT} 上启动服务器。应用程序即将退出。")
                # 使用QTimer在显示消息框后干净地退出
                QTimer.singleShot(0, self.quit_application)

    def toggle_script(self, script_id):
        """根据脚本的当前状态启动或停止它。"""
        if script_id in self.runner.processes and self.runner.processes[script_id]['process'].state() != QProcess.ProcessState.NotRunning:
            # 脚本正在运行，所以停止它
            self.runner.stop_script(script_id)
        else:
            # 脚本未运行，所以启动它
            script_info = self.tabs_info[script_id]
            self.runner.run_script(script_info['path'], script_info['args'])

    @Slot(str, str)
    def append_log_message(self, script_id, message):
        if script_id not in self.tabs_info:
            print(f"警告: 收到未知脚本ID的日志: {script_id}")
            return

        log_display = self.tabs_info[script_id]["log_display"]

        # --- 智能滚动逻辑 ---
        # 检查滚动条是否在底部，以决定是否需要自动滚动
        scrollbar = log_display.verticalScrollBar()
        is_at_bottom = scrollbar.value() >= scrollbar.maximum() - 5 # -5 作为容差

        # --- 字体处理 ---
        # 获取保存的字体设置
        en_font = self.settings.value("logFont_en", QFont())
        zh_font = self.settings.value("logFont_zh", QFont())

        # 获取文本光标并移动到文档末尾
        cursor = log_display.textCursor()
        cursor.movePosition(QTextCursor.End)

        # 检查是否是行内更新（如tqdm进度条）
        if message.startswith('\r'):
            # 这是一个行内更新
            # 移动到当前块（行）的开始
            cursor.movePosition(QTextCursor.MoveOperation.StartOfLine)
            # 选中到文档末尾（即选中当前最后一行）
            cursor.movePosition(QTextCursor.MoveOperation.End, QTextCursor.MoveMode.KeepAnchor)
            # 删除选中的文本
            cursor.removeSelectedText()
            # 加一个回车
            if message.endswith('\n'):
                message = message + '\n'
            self.insert_formatted_text(cursor, message, en_font, zh_font)
        else:
            # 这是普通日志或进度条的最后一次输出（通常带'\n'）
            # 直接插入文本，保留其原始格式
            self.insert_formatted_text(cursor, message, en_font, zh_font)
        
        # 如果之前就在底部，则新消息到来后继续滚动到底部。
        # 注意：ensureCursorVisible()有时因事件循环时序问题不可靠，直接操作滚动条更稳妥。
        if is_at_bottom:
            scrollbar.setValue(scrollbar.maximum())

    @Slot()
    def select_font(self, lang):
        """打开字体对话框以选择英文字体('en')或中文字体('zh')。"""
        current_tab_widget = self.tab_widget.currentWidget()
        if not current_tab_widget: return
        log_display = current_tab_widget.findChild(QTextEdit)
        if not log_display: return

        setting_key = f"logFont_{lang}"
        dialog_title = "选择英文字体" if lang == 'en' else "选择中文字体"

        # 从设置中加载当前字体，如果不存在则使用默认字体
        current_font = self.settings.value(setting_key, QFont())

        ok, font = QFontDialog.getFont(current_font, self, dialog_title)
        if ok:
            self.settings.setValue(setting_key, font)
            self.apply_fonts()

    def load_settings(self):
        """在应用程序启动时加载设置。"""
        self.apply_fonts()

    def apply_fonts(self):
        """将所选字体应用于所有日志显示区域。"""
        # 加载字体，如果未设置则使用默认值
        en_font = self.settings.value("logFont_en", QFont()) # 加载已保存的字体，如果找不到则使用默认值
        zh_font = self.settings.value("logFont_zh", QFont()) # 加载已保存的字体，如果找不到则使用默认值

        for info in self.tabs_info.values():
            log_display = info["log_display"]
            # 1. 设置基础字体为英文字体
            log_display.setFont(en_font)

            # 2. 重新渲染已有文本以应用中文字体
            # 获取所有现有文本，然后使用新的字体设置重新插入
            current_text = log_display.toPlainText()
            log_display.clear()
            cursor = log_display.textCursor()
            self.insert_formatted_text(cursor, current_text, en_font, zh_font)

    def insert_formatted_text(self, cursor, text, en_font, zh_font):
        """将文本插入QTextCursor，为中文字符和非中文字符应用不同的字体。"""
        import re
        # 正则表达式匹配中文字符
        chinese_char_pattern = re.compile(r'[\u4e00-\u9fa5]')

        # 将文本转换为HTML，为中文字符包裹特定的字体span
        html_parts = []
        for char in text:
            if chinese_char_pattern.match(char):
                # 对中文字符使用中文字体
                html_parts.append(f'<span style="font-family: \'{zh_font.family()}\';">{char}</span>')
            else:
                # 对非中文字符使用默认（英文）字体
                html_parts.append(char)
        
        # 插入HTML
        cursor.insertHtml("".join(html_parts).replace('\n', '<br>'))

    def show_error_message(self, script_id, message):
        self.tray_icon.showMessage("错误", message, QSystemTrayIcon.Critical)
        QApplication.beep()

    @Slot(str)
    def mark_tab_as_running(self, script_id):
        """将对应于script_id的标签页标记为正在运行（例如，红色文本）。"""
        if script_id in self.tabs_info:
            index = self.tabs_info[script_id]['index']
            button = self.tabs_info[script_id]['button']
            tab_name = self.tab_widget.tabText(index)
            button.setText(f"停止 {tab_name}")
            button.setStyleSheet("background-color: #FFDDDD; color: black;") # 淡红色背景，黑色文字
            self.tab_widget.tabBar().setTabTextColor(index, Qt.red)

    def mark_tab_as_finished(self, script_id):
        """当脚本完成时，将标签页颜色重置为默认值。"""
        if script_id in self.tabs_info:
            index = self.tabs_info[script_id]['index']
            button = self.tabs_info[script_id]['button']
            tab_name = self.tab_widget.tabText(index)
            button.setText(f"运行 {tab_name}")
            button.setStyleSheet("") # 恢复默认样式
            default_color = QApplication.palette().color(QPalette.ColorRole.WindowText)
            self.tab_widget.tabBar().setTabTextColor(index, default_color)

    @Slot(QSystemTrayIcon.ActivationReason)
    def on_tray_icon_activated(self, reason):
        """处理托盘图标激活事件，以在单击时显示窗口。"""
        # QSystemTrayIcon.Trigger 对应于鼠标左键单击。
        if reason == QSystemTrayIcon.Trigger:
            if self.isMinimized():
                self.setWindowState(self.windowState() & ~Qt.WindowState.WindowMinimized) # 恢复窗口状态
            elif not self.isVisible():
                self.show()
            self.raise_() # 将窗口置于顶层
            self.activateWindow() # 激活窗口

    def changeEvent(self, event):
        """重写changeEvent以处理最小化到托盘的操作。"""
        if event.type() == QEvent.WindowStateChange:
            # 检查窗口是否被最小化
            if self.windowState() & Qt.WindowState.WindowMinimized:
                event.ignore()  # 忽略默认的最小化事件
                self.hide()     # 隐藏窗口
                return
        super().changeEvent(event)

    def closeEvent(self, event):
        """当用户关闭窗口（点击X）时，退出应用程序。"""
        self.quit_application()
        event.accept()

    @Slot(str, str)
    def handle_script_finished(self, script_id, message):
        """处理脚本完成时的所有操作：重置标签页颜色并显示通知。"""
        self.mark_tab_as_finished(script_id)
        self.tray_icon.showMessage("任务完成", message, QSystemTrayIcon.MessageIcon.Information, 5000)
        QApplication.beep()

    def quit_application(self):
        """正确清理并退出应用程序。"""
        if self.server:
            self.server.stop()
        self.tray_icon.hide()
        QApplication.quit()


if __name__ == "__main__":
    # 在Windows上，设置AppUserModelID以确保任务栏图标正确。
    # 这必须在创建任何窗口之前完成。
    if sys.platform == "win32":
        import ctypes
        myappid = 'my.company.daemon.1.0'  # 任意唯一的字符串
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(myappid)

    app = QApplication(sys.argv)
    # 防止在最后一个窗口关闭时退出程序
    app.setQuitOnLastWindowClosed(False) 
    
    main_win = MainWindow()
    
    # 检查是否有 --hide 参数。如果存在，则启动后最小化到托盘。
    # 否则，显示窗口。
    if "--hide" not in sys.argv:
        main_win.show()
    
    sys.exit(app.exec())
