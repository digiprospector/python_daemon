# 守护程序主应用 (Fluent Design 版)
import sys
import re
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import shutil

from PySide6.QtCore import (QObject, Signal, Slot, QProcess, QTimer,
                            QProcessEnvironment, QEvent, Qt, QSettings,
                            QRegularExpression)
from PySide6.QtNetwork import QTcpServer, QHostAddress
from PySide6.QtWidgets import (QApplication, QSystemTrayIcon, QMenu, QStyle,
                               QVBoxLayout, QHBoxLayout, QWidget, QFontDialog,
                               QPlainTextEdit)
from PySide6.QtGui import (QIcon, QAction, QTextCursor, QFont, QFontMetrics,
                           QSyntaxHighlighter, QTextCharFormat, QColor)

from qfluentwidgets import (FluentWindow, NavigationItemPosition,
                            FluentIcon as FIF, PrimaryPushButton, PushButton,
                            TextEdit, setTheme, Theme, MessageBox,
                            SubtitleLabel, BodyLabel, StrongBodyLabel,
                            CardWidget)

# --- 配置文件检查 ---
# 在导入配置之前，检查 config.py 是否存在。如果不存在，则从 config_sample.py 复制。
CURRENT_SCRIPT_DIR = Path(__file__).parent
config_path = CURRENT_SCRIPT_DIR / "config.py"
sample_config_path = CURRENT_SCRIPT_DIR / "config_sample.py"

# --- 日志：所有 print(...) 输出统一写入 gui.log（不再打印到控制台） ---
LOG_FILE = CURRENT_SCRIPT_DIR / "gui.log"
_log_handler = RotatingFileHandler(
    LOG_FILE, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
)
_log_handler.setFormatter(logging.Formatter(
    "%(asctime)s %(levelname)-5s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
))
_logger = logging.getLogger("daemon")
_logger.setLevel(logging.DEBUG)
_logger.addHandler(_log_handler)
_logger.propagate = False  # 不冒泡到 root logger，避免再写控制台


def print(*args, file=None, sep=" ", end="\n", flush=False):
    """覆盖内置 print：所有调用改为写入 gui.log。
    保留了 file=sys.stderr 的语义（写为 ERROR 级别）。"""
    msg = sep.join(str(a) for a in args)
    # 去掉末尾换行，logging 自带换行
    if msg.endswith("\n"):
        msg = msg.rstrip("\n")
    # 注意：在 pythonw.exe 下 sys.stderr 可能是 None，不能直接用 `is sys.stderr`
    # 比较，否则默认 file=None 会被误判为 stderr，所有日志都进 ERROR。
    if file is not None and file is sys.stderr:
        _logger.error(msg)
    else:
        _logger.info(msg)


if not config_path.exists():
    print(f"'config.py' 未找到。正在尝试从 '{sample_config_path.name}' 创建...")
    if sample_config_path.exists():
        try:
            shutil.copy(sample_config_path, config_path)
            print(f"成功创建 'config.py'。请根据您的需求编辑该文件后重新启动程序。")
        except Exception as e:
            print(f"错误: 从模板创建 'config.py' 失败: {e}")
            sys.exit(1)
    else:
        print(f"错误: '{config_path.name}' 和 '{sample_config_path.name}' 都不存在。程序无法启动。")
        sys.exit(1)

# --- 配置 ---
from config import SCRIPTS_CONFIG, HOST, PORT, ENABLE_TCP_SERVER

def _resolve_child_python(exe: str) -> str:
    """子脚本必须用 python.exe（不是 pythonw.exe）。
    pythonw.exe 下 sys.stdout/stderr 是 None，子脚本第一条 print 就会
    抛 AttributeError，QProcess 把 stderr 吞掉，于是现象就是"点了没反应"。
    """
    if sys.platform == "win32" and exe.lower().endswith("pythonw.exe"):
        candidate = Path(exe).with_name("python.exe")
        if candidate.exists():
            return str(candidate)
    return exe


PYTHON_EXECUTABLE = _resolve_child_python(sys.executable)
# ---------------------

# 侧边栏图标轮转池：尽量挑选视觉差异较大的，避免相邻项混淆
NAV_ICON_POOL = [
    FIF.VIDEO,
    FIF.MARKET,
    FIF.BROOM,
    FIF.CODE,
    FIF.GLOBE,
    FIF.MUSIC,
    FIF.ROBOT,
    FIF.MAIL,
    FIF.GAME,
    FIF.BOOK_SHELF,
    FIF.PIE_SINGLE,
    FIF.HOME,
    FIF.CLOUD,
    FIF.MEGAPHONE,
]


def resolve_nav_icon(cfg: dict, index: int):
    """选择一个用于侧边栏的图标。

    1. 若 config 里指定了 'icon' 字段：
       - 是 FluentIcon 成员，直接用
       - 是字符串（如 "VIDEO"），按名查 FluentIcon
    2. 否则按位置从轮转池循环取，保证每个脚本图标不同
    """
    icon = cfg.get('icon')
    if isinstance(icon, FIF):
        return icon
    if isinstance(icon, str):
        candidate = getattr(FIF, icon.strip().upper(), None)
        if candidate is not None:
            return candidate
    return NAV_ICON_POOL[index % len(NAV_ICON_POOL)]



class ScriptRunner(QObject):
    """处理外部脚本的运行，允许多个脚本并发执行。"""
    setup_error = Signal(str, str)        # 脚本ID, 消息
    log_message = Signal(str, str)        # 脚本ID, 消息
    started_message = Signal(str)         # 脚本ID
    finished_message = Signal(str, str)   # 脚本ID, 消息

    def __init__(self, parent=None):
        super().__init__(parent)
        self.processes = {}  # 脚本ID -> {process: QProcess, name: str}

    @Slot(str)
    def run_script(self, script_path_str, args=None):
        script_path = Path(script_path_str)
        # 注意：absolute() 不解析 ".."；这里继续保留与 Page.script_id 一致的写法
        script_id = str(script_path.absolute())

        # === 调试日志 (1/4): 入口（仅写入文件，不进 GUI 日志框） ===
        debug_lines = [
            "========== 触发运行 ==========",
            f"  传入路径 (原始): {script_path_str}",
            f"  绝对化路径      : {script_id}",
        ]
        try:
            resolved = str(script_path.resolve(strict=False))
            debug_lines.append(f"  resolve() 后    : {resolved}")
        except Exception as e:
            debug_lines.append(f"  resolve() 失败  : {e}")
        debug_lines.append(f"  附加参数 args   : {args!r}")
        debug_lines.append(f"  Python 解释器   : {PYTHON_EXECUTABLE}")
        debug_lines.append(f"  当前工作目录    : {Path.cwd()}")
        debug_msg = "\n".join(debug_lines) + "\n"
        print(debug_msg)

        if script_id in self.processes and self.processes[script_id]['process'].state() != QProcess.ProcessState.NotRunning:
            self.log_message.emit(script_id, "(已有同脚本进程在运行 → 改为停止)\n")
            self.stop_script(script_id)
            return True

        # === 调试日志 (2/4): 路径检查（仅文件） ===
        exists = script_path.exists()
        is_file = script_path.is_file() if exists else False
        print(f"  路径存在性: exists={exists}, is_file={is_file}")

        if not exists:
            error_msg = (
                f"错误: 脚本未找到。\n"
                f"  原始路径: {script_path_str}\n"
                f"  绝对路径: {script_id}\n"
                f"  请检查 config.py 里的路径，相对路径以 config.py 所在目录为基准。\n"
            )
            print(error_msg)
            self.setup_error.emit(script_id, error_msg)
            self.log_message.emit(script_id, error_msg)
            return False

        arguments = [script_id]
        if args:
            arguments.extend(str(a) for a in args)

        working_dir = str(script_path.absolute().parent)

        # === 调试日志 (3/4): 启动前 ===
        # 详细命令行只写入文件，不进 GUI
        print(
            f"  工作目录: {working_dir}\n"
            f"  完整命令: {PYTHON_EXECUTABLE} "
            f"{' '.join(repr(a) for a in arguments)}"
        )
        # GUI 日志框只显示这一行简短的开始提示
        self.log_message.emit(
            script_id, f"--- 开始运行脚本: {script_path.name} ---\n"
        )
        self.started_message.emit(script_id)

        process = QProcess()
        self.processes[script_id] = {'process': process, 'name': script_path.name}
        process.setWorkingDirectory(working_dir)

        # 强制 UTF-8 + 关闭缓冲
        env = QProcessEnvironment.systemEnvironment()
        env.insert("PYTHONIOENCODING", "utf-8")
        env.insert("PYTHONUNBUFFERED", "1")
        process.setProcessEnvironment(env)

        # Windows: 用 CREATE_NO_WINDOW 防止 python.exe 弹出黑色控制台窗口
        if sys.platform == "win32":
            def _no_console(qpa):
                qpa.flags |= 0x08000000  # CREATE_NO_WINDOW
            try:
                process.setCreateProcessArgumentsModifier(_no_console)
            except AttributeError:
                pass  # 旧版 PySide6 没有此 API，忽略

        process.readyReadStandardOutput.connect(lambda: self.handle_stdout(script_id))
        process.readyReadStandardError.connect(lambda: self.handle_stderr(script_id))
        process.finished.connect(lambda code, status: self.on_finished(script_id, code, status))
        # 捕获启动失败、找不到可执行文件等无法走 stdout/stderr 的错误
        process.errorOccurred.connect(lambda err: self.on_process_error(script_id, err))

        process.start(PYTHON_EXECUTABLE, arguments)

        # === 调试日志 (4/4): 启动后状态确认（仅文件） ===
        started = process.waitForStarted(1500)
        state_name = {
            QProcess.ProcessState.NotRunning: "NotRunning",
            QProcess.ProcessState.Starting:   "Starting",
            QProcess.ProcessState.Running:    "Running",
        }.get(process.state(), str(process.state()))
        print(
            f"  waitForStarted(1500ms): {started}\n"
            f"  当前 ProcessState: {state_name}\n"
            f"  PID: {process.processId()}"
        )

        if not started:
            err_text = process.errorString()
            self.log_message.emit(
                script_id,
                f"!! 进程未能在 1.5s 内启动: {err_text}\n"
            )

        return True

    @Slot(str, object)
    def on_process_error(self, script_id, error):
        """捕获 QProcess.errorOccurred 信号。"""
        names = {
            QProcess.ProcessError.FailedToStart: "FailedToStart (找不到/无权限)",
            QProcess.ProcessError.Crashed:       "Crashed",
            QProcess.ProcessError.Timedout:      "Timedout",
            QProcess.ProcessError.WriteError:    "WriteError",
            QProcess.ProcessError.ReadError:     "ReadError",
            QProcess.ProcessError.UnknownError:  "UnknownError",
        }
        err_name = names.get(error, str(error))
        proc = self.processes.get(script_id, {}).get('process')
        err_text = proc.errorString() if proc else "(无 process 句柄)"
        msg = f"!! QProcess 错误: {err_name}\n   详情: {err_text}\n"
        print(msg)
        self.log_message.emit(script_id, msg)

    @Slot(str)
    def stop_script(self, script_id):
        if script_id in self.processes and self.processes[script_id]['process'].state() != QProcess.ProcessState.NotRunning:
            self.log_message.emit(script_id, f"--- 正在终止脚本: {self.processes[script_id]['name']} ---\n")
            self.processes[script_id]['process'].kill()
            return True
        return False

    def handle_stdout(self, script_id):
        if script_id in self.processes:
            data = self.processes[script_id]['process'].readAllStandardOutput().data().decode('utf-8', errors='ignore')
            self.log_message.emit(script_id, data)

    def handle_stderr(self, script_id):
        if script_id in self.processes:
            data = self.processes[script_id]['process'].readAllStandardError().data().decode('utf-8', errors='ignore')
            self.log_message.emit(script_id, data)

    def on_finished(self, script_id, exit_code, exit_status):
        status_text = "正常退出" if exit_status == QProcess.ExitStatus.NormalExit else "崩溃"
        if script_id in self.processes:
            script_name = self.processes[script_id]['name']
        else:
            script_name = Path(script_id).name
        finish_msg = (
            f"--- 脚本运行结束: {script_name} "
            f"(退出码: {exit_code}, 状态: {status_text}) ---"
        )
        print(finish_msg)
        self.log_message.emit(script_id, "\n" + finish_msg + "\n")
        self.finished_message.emit(
            script_id,
            f"{script_name} 脚本运行结束 (退出码: {exit_code}, 状态: {status_text})"
        )
        if script_id in self.processes:
            del self.processes[script_id]


class Server(QObject):
    """简单的 TCP 服务器，用于监听特定消息。"""
    trigger_script = Signal(str, list)  # 触发信号，参数为脚本路径和参数列表

    def __init__(self, parent=None):
        super().__init__(parent)
        self._server = QTcpServer(self)
        self._server.newConnection.connect(self.on_new_connection)
        self.message_map = {
            cfg['msg'].decode('utf-8'): {
                'script': cfg['script'],
                'args': cfg.get('args', []),
            }
            for cfg in SCRIPTS_CONFIG
        }

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

    def on_ready_read(self, socket):
        data = socket.readAll().data().decode('utf-8').strip()
        print(f"收到数据: {data}")
        if data in self.message_map:
            info = self.message_map[data]
            print(f"触发脚本: {info['script']} 参数: {info['args']}")
            self.trigger_script.emit(info['script'], info['args'])
            socket.write(f"确认: 已触发 {Path(info['script']).name}。\n".encode('utf-8'))
        else:
            socket.write("错误: 无效消息。\n".encode('utf-8'))
        socket.disconnectFromHost()


def _slugify_object_name(s: str) -> str:
    """生成一个对 Qt objectName 安全的字符串。"""
    # 用稳定 hash 简化（标准 hash 在不同进程间不同，但同进程内一致）
    return "page_" + re.sub(r'[^A-Za-z0-9_]', '_', s)[-40:] + f"_{abs(hash(s))}"


class ScriptPage(QWidget):
    """单个脚本对应的页面（侧边栏每一项打开这样一个页面）。"""
    toggle_requested = Signal(str)  # 发出 script_id

    def __init__(self, script_id, name, path, args, parent=None):
        super().__init__(parent)
        self.script_id = script_id
        self.script_name = name
        self.script_path = path
        self.script_args = args

        # FluentWindow 用 objectName 作为切换 key，必须唯一
        self.setObjectName(_slugify_object_name(script_id))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 20, 28, 20)
        layout.setSpacing(12)

        # 标题行
        title_row = QHBoxLayout()
        self.title_label = SubtitleLabel(name)
        self.status_label = StrongBodyLabel("● 就绪")
        self.status_label.setStyleSheet("color: #888888;")
        title_row.addWidget(self.title_label)
        title_row.addStretch()
        title_row.addWidget(self.status_label)
        layout.addLayout(title_row)

        # 脚本信息卡片
        info_card = CardWidget()
        info_layout = QVBoxLayout(info_card)
        info_layout.setContentsMargins(16, 12, 16, 12)
        info_layout.setSpacing(4)
        info_layout.addWidget(BodyLabel(f"脚本: {path}"))
        if args:
            info_layout.addWidget(BodyLabel(f"参数: {' '.join(map(str, args))}"))
        layout.addWidget(info_card)

        # 运行 / 停止按钮
        self.run_button = PrimaryPushButton(FIF.PLAY, f"运行 {name}")
        self.run_button.clicked.connect(
            lambda: self.toggle_requested.emit(self.script_id)
        )
        layout.addWidget(self.run_button)

        # 日志区
        self.log_display = TextEdit()
        self.log_display.setReadOnly(True)
        self.log_display.setLineWrapMode(TextEdit.LineWrapMode.NoWrap)
        layout.addWidget(self.log_display, 1)

    def set_running(self, running: bool):
        if running:
            self.run_button.setText(f"停止 {self.script_name}")
            self.run_button.setIcon(FIF.PAUSE)
            self.status_label.setText("● 运行中")
            self.status_label.setStyleSheet("color: #d73a49;")
        else:
            self.run_button.setText(f"运行 {self.script_name}")
            self.run_button.setIcon(FIF.PLAY)
            self.status_label.setText("● 就绪")
            self.status_label.setStyleSheet("color: #888888;")

    def append_log(self, message: str, log_font: QFont):
        scrollbar = self.log_display.verticalScrollBar()
        is_at_bottom = scrollbar.value() >= scrollbar.maximum() - 5

        cursor = self.log_display.textCursor()
        cursor.movePosition(QTextCursor.End)

        # 应用统一字体（不再分中英文）
        char_format = cursor.charFormat()
        char_format.setFont(log_font)
        cursor.setCharFormat(char_format)

        if message.startswith('\r'):
            # 进度条行内更新：覆盖最后一行
            cursor.movePosition(QTextCursor.MoveOperation.StartOfLine)
            cursor.movePosition(
                QTextCursor.MoveOperation.End,
                QTextCursor.MoveMode.KeepAnchor
            )
            cursor.removeSelectedText()
            cursor.insertText(message.lstrip('\r'))
        else:
            cursor.insertText(message)

        if is_at_bottom:
            scrollbar.setValue(scrollbar.maximum())


# ---------- Python 语法高亮（极简版） ----------
class PythonHighlighter(QSyntaxHighlighter):
    KEYWORDS = [
        'False', 'None', 'True', 'and', 'as', 'assert', 'async', 'await',
        'break', 'class', 'continue', 'def', 'del', 'elif', 'else', 'except',
        'finally', 'for', 'from', 'global', 'if', 'import', 'in', 'is',
        'lambda', 'nonlocal', 'not', 'or', 'pass', 'raise', 'return', 'try',
        'while', 'with', 'yield',
    ]

    def __init__(self, parent=None, dark: bool = False):
        super().__init__(parent)
        self.rules = []

        # 颜色根据主题切换：暗色用更亮的色，浅色用偏深的色
        c_kw     = "#569CD6" if dark else "#0033B3"
        c_str    = "#CE9178" if dark else "#067D17"
        c_num    = "#B5CEA8" if dark else "#1750EB"
        c_cmt    = "#6A9955" if dark else "#8C8C8C"
        c_def    = "#DCDCAA" if dark else "#7A3E9D"
        c_decor  = "#C586C0" if dark else "#9E880D"

        # 关键字
        kw_fmt = QTextCharFormat()
        kw_fmt.setForeground(QColor(c_kw))
        kw_fmt.setFontWeight(QFont.Weight.Bold)
        for kw in self.KEYWORDS:
            self.rules.append((QRegularExpression(rf'\b{kw}\b'), kw_fmt))

        # 函数/类定义后的标识符
        def_fmt = QTextCharFormat()
        def_fmt.setForeground(QColor(c_def))
        self.rules.append(
            (QRegularExpression(r'\b(?:def|class)\s+([A-Za-z_]\w*)'), def_fmt)
        )

        # 装饰器
        dec_fmt = QTextCharFormat()
        dec_fmt.setForeground(QColor(c_decor))
        self.rules.append((QRegularExpression(r'@[A-Za-z_]\w*'), dec_fmt))

        # 数字
        num_fmt = QTextCharFormat()
        num_fmt.setForeground(QColor(c_num))
        self.rules.append((QRegularExpression(r'\b\d+(?:\.\d+)?\b'), num_fmt))

        # 字符串（单/双引号，带 b/r/f 前缀，不跨行）
        str_fmt = QTextCharFormat()
        str_fmt.setForeground(QColor(c_str))
        self.rules.append((QRegularExpression(
            r'[bBrRuUfF]{0,2}"(?:\\.|[^"\\])*"'
        ), str_fmt))
        self.rules.append((QRegularExpression(
            r"[bBrRuUfF]{0,2}'(?:\\.|[^'\\])*'"
        ), str_fmt))

        # 注释（必须最后，覆盖其他规则）
        cmt_fmt = QTextCharFormat()
        cmt_fmt.setForeground(QColor(c_cmt))
        cmt_fmt.setFontItalic(True)
        self.rules.append((QRegularExpression(r'#[^\n]*'), cmt_fmt))

    def highlightBlock(self, text: str):
        for regex, fmt in self.rules:
            it = regex.globalMatch(text)
            while it.hasNext():
                m = it.next()
                # 若有捕获组（如 def foo），仅高亮捕获组；否则整体
                if m.lastCapturedIndex() >= 1:
                    self.setFormat(
                        m.capturedStart(1),
                        m.capturedLength(1),
                        fmt,
                    )
                else:
                    self.setFormat(m.capturedStart(), m.capturedLength(), fmt)


class ConfigEditorPage(QWidget):
    """在应用内编辑 config.py：语法检查 → 备份 → 写入 → 提示重启。"""
    restart_requested = Signal()

    def __init__(self, config_file_path: Path, parent=None):
        super().__init__(parent)
        self.setObjectName("configEditorPage")
        self.config_file_path = config_file_path

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 20, 28, 20)
        layout.setSpacing(12)

        # 标题行 + 状态
        title_row = QHBoxLayout()
        title_row.addWidget(SubtitleLabel("配置编辑"))
        title_row.addStretch()
        self.status_label = StrongBodyLabel("")
        title_row.addWidget(self.status_label)
        layout.addLayout(title_row)

        layout.addWidget(BodyLabel(f"文件: {config_file_path}"))
        layout.addWidget(BodyLabel(
            "提示：保存前会做 Python 语法检查；保存时会先备份为 config.py.bak。"
            "修改后需要重启应用才会生效。"
        ))

        # 工具栏
        toolbar = QHBoxLayout()
        self.reload_btn = PushButton(FIF.SYNC, "重新加载")
        self.reload_btn.clicked.connect(self.reload_from_disk)
        self.save_btn = PrimaryPushButton(FIF.SAVE, "保存")
        self.save_btn.clicked.connect(self.save_to_disk)
        self.restart_btn = PushButton(FIF.UPDATE, "保存并重启")
        self.restart_btn.clicked.connect(self.save_and_restart)
        toolbar.addWidget(self.reload_btn)
        toolbar.addWidget(self.save_btn)
        toolbar.addStretch()
        toolbar.addWidget(self.restart_btn)
        layout.addLayout(toolbar)

        # 编辑器
        self.editor = QPlainTextEdit()
        mono = QFont("Consolas", 11)
        mono.setStyleHint(QFont.StyleHint.Monospace)
        self.editor.setFont(mono)
        self.editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        # Tab 宽度 = 4 个空格
        self.editor.setTabStopDistance(
            4 * QFontMetrics(mono).horizontalAdvance(' ')
        )
        # 跟随主题判断深色
        is_dark = QApplication.palette().color(
            QApplication.palette().ColorRole.Window
        ).lightness() < 128
        self.highlighter = PythonHighlighter(self.editor.document(), dark=is_dark)
        layout.addWidget(self.editor, 1)

        self.reload_from_disk(notify=False)

    def reload_from_disk(self, notify: bool = True):
        try:
            text = self.config_file_path.read_text(encoding='utf-8')
            self.editor.setPlainText(text)
            if notify:
                self._set_status("已从磁盘重新加载", ok=True)
        except Exception as e:
            self._set_status(f"加载失败: {e}", ok=False)

    def _validate_and_write(self) -> bool:
        text = self.editor.toPlainText()
        # 1. 语法检查
        try:
            compile(text, str(self.config_file_path), 'exec')
        except SyntaxError as e:
            self._set_status(f"语法错误 (行 {e.lineno}): {e.msg}", ok=False)
            w = MessageBox(
                "语法错误",
                f"第 {e.lineno} 行: {e.msg}\n\n请修复后再保存。",
                self.window(),
            )
            w.exec()
            return False
        # 2. 备份
        try:
            backup = self.config_file_path.with_suffix('.py.bak')
            if self.config_file_path.exists():
                shutil.copy(self.config_file_path, backup)
            self.config_file_path.write_text(text, encoding='utf-8')
        except Exception as e:
            self._set_status(f"保存失败: {e}", ok=False)
            return False
        return True

    def save_to_disk(self):
        if self._validate_and_write():
            self._set_status("已保存（重启应用后生效）", ok=True)

    def save_and_restart(self):
        if not self._validate_and_write():
            return
        # 询问确认
        w = MessageBox(
            "保存并重启",
            "配置已保存。是否立即重启应用以应用新配置？",
            self.window(),
        )
        if w.exec():
            self.restart_requested.emit()

    def _set_status(self, msg: str, ok: bool = True):
        self.status_label.setText(msg)
        self.status_label.setStyleSheet(
            "color: #2ea043;" if ok else "color: #d73a49;"
        )


class SettingPage(QWidget):
    """设置页：字体 + 主题。"""
    font_changed = Signal(QFont)
    theme_changed = Signal(str)  # 'auto' | 'light' | 'dark'

    def __init__(self, current_font: QFont, current_theme_key: str, parent=None):
        super().__init__(parent)
        self.setObjectName("settingPage")
        self.current_font = current_font

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 20, 28, 20)
        layout.setSpacing(16)

        layout.addWidget(SubtitleLabel("设置"))

        # 字体卡片
        font_card = CardWidget()
        font_layout = QVBoxLayout(font_card)
        font_layout.setContentsMargins(16, 12, 16, 12)
        font_layout.setSpacing(8)
        font_layout.addWidget(StrongBodyLabel("日志字体"))
        self.font_label = BodyLabel(self._format_font_label(current_font))
        font_layout.addWidget(self.font_label)
        font_btn = PushButton(FIF.FONT, "选择字体...")
        font_btn.clicked.connect(self.on_select_font)
        font_layout.addWidget(font_btn, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(font_card)

        # 主题卡片
        theme_card = CardWidget()
        theme_layout = QVBoxLayout(theme_card)
        theme_layout.setContentsMargins(16, 12, 16, 12)
        theme_layout.setSpacing(8)
        theme_layout.addWidget(StrongBodyLabel("外观主题"))
        theme_layout.addWidget(BodyLabel("默认跟随系统设置；如需固定可手动选择。"))
        theme_row = QHBoxLayout()
        theme_row.setSpacing(8)
        for label, key in [("跟随系统", "auto"), ("浅色", "light"), ("深色", "dark")]:
            btn = PushButton(label)
            btn.clicked.connect(lambda _=False, k=key: self.theme_changed.emit(k))
            theme_row.addWidget(btn)
        theme_row.addStretch()
        theme_layout.addLayout(theme_row)
        layout.addWidget(theme_card)

        # 关于
        about_card = CardWidget()
        about_layout = QVBoxLayout(about_card)
        about_layout.setContentsMargins(16, 12, 16, 12)
        about_layout.addWidget(StrongBodyLabel("关于"))
        about_layout.addWidget(BodyLabel("python_daemon - 后台脚本守护程序 (Fluent UI)"))
        about_layout.addWidget(BodyLabel(f"监听 {HOST}:{PORT}（可在 config.py 调整）"))
        layout.addWidget(about_card)

        layout.addStretch()

    def _format_font_label(self, font: QFont) -> str:
        size = font.pointSize() if font.pointSize() > 0 else font.pixelSize()
        return f"当前: {font.family()}  {size}pt"

    def on_select_font(self):
        # 不使用静态方法 getFont（不同 PySide6 版本返回元组顺序不一致），
        # 改用对话框实例，行为可移植、更明确。
        dialog = QFontDialog(self.current_font, self)
        dialog.setWindowTitle("选择日志字体")
        if dialog.exec() == QFontDialog.DialogCode.Accepted:
            font = dialog.selectedFont()
            self.current_font = font
            self.font_label.setText(self._format_font_label(font))
            self.font_changed.emit(font)


class MainWindow(FluentWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("后台脚本守护程序")
        self.resize(1000, 680)

        # --- 设置存储 ---
        self.settings = QSettings("my_company", "daemon_gui_fluent")

        # --- 字体加载 ---
        saved_font = self.settings.value("logFont")
        if isinstance(saved_font, QFont):
            self.log_font = saved_font
        else:
            # 优先使用 Consolas / 等宽字体
            self.log_font = QFont("Consolas", 10)

        # --- 主题加载（默认跟随系统） ---
        theme_key = self.settings.value("theme", "auto")
        if theme_key not in ("auto", "light", "dark"):
            theme_key = "auto"
        self._apply_theme(theme_key, persist=False)

        # --- 创建脚本页面并添加到侧边栏 ---
        self.pages = {}  # script_id -> ScriptPage
        for index, cfg in enumerate(SCRIPTS_CONFIG):
            script_path = cfg['script']
            script_id = str(Path(script_path).absolute())
            name = cfg['name']
            args = cfg.get('args', [])

            page = ScriptPage(script_id, name, script_path, args)
            page.toggle_requested.connect(self.toggle_script)
            page.log_display.setFont(self.log_font)
            self.pages[script_id] = page

            self.addSubInterface(page, resolve_nav_icon(cfg, index), name)

        # --- 配置编辑页（侧边栏底部）---
        self.config_editor_page = ConfigEditorPage(config_path)
        self.config_editor_page.restart_requested.connect(self.restart_application)
        self.addSubInterface(
            self.config_editor_page, FIF.DOCUMENT, "配置编辑",
            position=NavigationItemPosition.BOTTOM
        )

        # --- 设置页（侧边栏底部）---
        self.setting_page = SettingPage(self.log_font, theme_key)
        self.setting_page.font_changed.connect(self.on_font_changed)
        self.setting_page.theme_changed.connect(self.on_theme_changed)
        self.addSubInterface(
            self.setting_page, FIF.SETTING, "设置",
            position=NavigationItemPosition.BOTTOM
        )

        # --- 应用图标 ---
        icon_path = CURRENT_SCRIPT_DIR / "icon.png"
        if icon_path.exists():
            app_icon = QIcon(str(icon_path))
        else:
            app_icon = QApplication.style().standardIcon(QStyle.SP_ComputerIcon)
        self.setWindowIcon(app_icon)

        # --- 系统托盘 ---
        self.tray_icon = QSystemTrayIcon(self)
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
        self.runner = ScriptRunner()
        self.runner.setup_error.connect(self.show_error_message)
        self.runner.started_message.connect(self.mark_running)
        self.runner.log_message.connect(self.append_log)
        self.runner.finished_message.connect(self.handle_script_finished)

        self.server = None
        if ENABLE_TCP_SERVER:
            self.server = Server()
            self.server.trigger_script.connect(self.runner.run_script)
            if not self.server.start():
                w = MessageBox("服务器错误",
                               f"无法在端口 {PORT} 上启动服务器，应用程序即将退出。",
                               self)
                w.exec()
                QTimer.singleShot(0, self.quit_application)

    # ---------- 主题与字体 ----------
    def _apply_theme(self, key: str, persist: bool = True):
        mapping = {"auto": Theme.AUTO, "dark": Theme.DARK, "light": Theme.LIGHT}
        setTheme(mapping.get(key, Theme.AUTO))
        if persist:
            self.settings.setValue("theme", key)

    @Slot(str)
    def on_theme_changed(self, key: str):
        self._apply_theme(key)

    @Slot(QFont)
    def on_font_changed(self, font: QFont):
        self.log_font = font
        self.settings.setValue("logFont", font)
        # 同时更新基础字体 + 对已有文本重新应用 charFormat（否则旧日志看起来没变）
        for page in self.pages.values():
            log = page.log_display
            log.setFont(font)
            cursor = log.textCursor()
            saved_pos = cursor.position()
            cursor.select(QTextCursor.SelectionType.Document)
            fmt = cursor.charFormat()
            fmt.setFont(font)
            cursor.mergeCharFormat(fmt)
            cursor.clearSelection()
            cursor.setPosition(saved_pos)
            log.setTextCursor(cursor)

    # ---------- 脚本控制 ----------
    def toggle_script(self, script_id: str):
        # 调试入口
        already = (script_id in self.runner.processes and
                   self.runner.processes[script_id]['process'].state() != QProcess.ProcessState.NotRunning)
        print(f"[toggle_script] script_id={script_id!r}  already_running={already}")
        if script_id not in self.pages:
            print(f"[toggle_script] !! script_id 不在 self.pages 中，已知键: "
                  f"{list(self.pages.keys())}")
            return
        if already:
            self.runner.stop_script(script_id)
        else:
            page = self.pages[script_id]
            print(f"[toggle_script] page.script_path={page.script_path!r} args={page.script_args!r}")
            self.runner.run_script(page.script_path, page.script_args)

    @Slot(str, str)
    def append_log(self, script_id: str, message: str):
        if script_id in self.pages:
            self.pages[script_id].append_log(message, self.log_font)
        else:
            print(f"警告: 收到未知脚本ID的日志: {script_id}")

    @Slot(str)
    def mark_running(self, script_id: str):
        if script_id in self.pages:
            self.pages[script_id].set_running(True)

    def mark_finished(self, script_id: str):
        if script_id in self.pages:
            self.pages[script_id].set_running(False)

    @Slot(str, str)
    def show_error_message(self, script_id: str, message: str):
        print(f"[ERROR] {script_id}\n{message}", file=sys.stderr)
        self.tray_icon.showMessage("错误", message, QSystemTrayIcon.Critical)
        QApplication.beep()

    @Slot(str, str)
    def handle_script_finished(self, script_id: str, message: str):
        self.mark_finished(script_id)
        self.tray_icon.showMessage(
            "任务完成", message,
            QSystemTrayIcon.MessageIcon.Information, 5000
        )
        QApplication.beep()

    # ---------- 托盘 / 窗口事件 ----------
    @Slot(QSystemTrayIcon.ActivationReason)
    def on_tray_icon_activated(self, reason):
        if reason == QSystemTrayIcon.Trigger:
            if self.isMinimized():
                self.setWindowState(self.windowState() & ~Qt.WindowState.WindowMinimized)
            elif not self.isVisible():
                self.show()
            self.raise_()
            self.activateWindow()

    def changeEvent(self, event):
        if event.type() == QEvent.WindowStateChange:
            if self.windowState() & Qt.WindowState.WindowMinimized:
                event.ignore()
                self.hide()
                return
        super().changeEvent(event)

    def closeEvent(self, event):
        self.quit_application()
        event.accept()

    def quit_application(self):
        if self.server:
            self.server.stop()
        self.tray_icon.hide()
        QApplication.quit()

    def restart_application(self):
        """重新启动应用本身（用于配置热生效）。"""
        # 先停服务，避免端口未释放导致新进程启动失败
        if self.server:
            self.server.stop()
        self.tray_icon.hide()

        script_path = str(Path(__file__).resolve())
        # 保留除 --hide 外的命令行参数（重启后默认显示窗口，避免误以为没启动）
        extra_args = [a for a in sys.argv[1:] if a != "--hide"]
        QProcess.startDetached(PYTHON_EXECUTABLE, [script_path] + extra_args)
        QApplication.quit()


if __name__ == "__main__":
    print(f"========== gui.py 启动 ==========")
    print(f"  sys.executable          : {sys.executable}")
    print(f"  PYTHON_EXECUTABLE (子进程): {PYTHON_EXECUTABLE}")
    print(f"  argv                    : {sys.argv}")

    if sys.platform == "win32":
        import ctypes
        myappid = 'my.company.daemon.fluent.1.0'
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(myappid)

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    main_win = MainWindow()
    if "--hide" not in sys.argv:
        main_win.show()

    sys.exit(app.exec())
