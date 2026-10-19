"""
7risha.com 每日任务奖励自动领取脚本

站点形态：WordPress + LightSNS 主题
签到形式：POST /wp-content/themes/LightSNS/module/action/task.php
         表单 task_id=taskXXX&type=day
登录态：Cookie 中的 wordpress_logged_in_<hash>
"""

import os
import sys
import time
from collections import OrderedDict
from dataclasses import asdict, dataclass
from enum import Enum
from functools import wraps
from typing import Dict, List, Optional, Tuple

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from logging_config import init_logger


class ClaimStatus(Enum):
    """领取状态（内部枚举，与站点返回码解耦，避免被远端码值绑架）"""

    SUCCESS = 0
    REPEAT = 1
    FAILURE = -1


class Endpoint(Enum):
    """站点接口"""

    HOME = "/"
    SIGN = "/wp-content/themes/LightSNS/module/action/sign.php"
    TASK = "/wp-content/themes/LightSNS/module/action/task.php"


class LogEmoji:
    """日志 Emoji 常量"""

    SUCCESS = "✅"
    FAIL = "❌"
    REPEAT = "🔄"
    SKIP = "⏭️"
    WAIT = "⏳"
    SIGN = "📝"
    TASK = "🎯"
    START = "🚀"
    END = "🏁"
    COOKIE = "🍪"
    SITE = "🌐"
    WARNING = "⚠️"
    ERROR = "🔴"
    INFO = "ℹ️"


def log_method(func):
    """日志装饰器：异常不向上抛，按方法返回类型化默认值，保证单个账号失败不中断整体"""

    @wraps(func)
    def wrapper(self, *args, **kwargs):
        method_name = func.__name__
        emoji = {"claim_task": LogEmoji.TASK, "sign": LogEmoji.SIGN, "warmup": LogEmoji.SITE}.get(method_name, LogEmoji.INFO)
        try:
            return func(self, *args, **kwargs)
        except Exception as e:
            logger.error(f"{LogEmoji.COOKIE}[{self.cookie_index}] {LogEmoji.SITE}[{self.domain}] {LogEmoji.ERROR} {emoji} {method_name} 执行失败: {e}")

            if method_name == "claim_task":
                task_id = args[0] if args else kwargs.get("task_id", "unknown")
                return self._fail_result(str(task_id), f"执行失败: {e}")
            if method_name == "sign":
                return self._fail_result("", f"执行失败: {e}", "签到失败")
            if method_name == "warmup":
                return False
            raise

    return wrapper


def parse_cookie_header(raw: str) -> "OrderedDict[str, str]":
    """把浏览器复制的 Cookie 头解析为有序字典

    只按第一个 '=' 切分，因为 WordPress 的 cookie 值里含 '=' 的 base64 补位。
    """
    jar: "OrderedDict[str, str]" = OrderedDict()
    for part in raw.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, value = part.split("=", 1)
        name = name.strip()
        if name:
            jar[name] = value.strip()
    return jar


class Config:
    """应用配置"""

    ENV_COOKIES = "COOKIES"
    ENV_PUSH_KEY = "PUSHDEER_SENDKEY"
    ENV_SERVERCHAN_KEY = "SERVERCHAN_SENDKEY"
    ENV_VERBOSE = "VERBOSE"

    """站点域名"""
    DOMAIN = "www.7risha.com"

    """是否先执行每日签到（sign.php），签到会写入累计天数"""
    SIGN_ENABLED = True

    """两个每日任务，固定领取，不做配置化"""
    TASKS = ["task007", "task009"]

    """每日任务的 type 值"""
    TASK_TYPE = "day"

    """每个动作之间的间隔（秒），避免短时间连发请求"""
    ACTION_INTERVAL = 5

    """默认是否输出详细响应"""
    DEFAULT_VERBOSE = False

    def __init__(self):
        self.push_key: str = ""
        self.serverchan_key: str = ""
        self.cookies_list: List[str] = []
        self.sign_enabled: bool = self.SIGN_ENABLED
        self.tasks: List[str] = list(self.TASKS)
        self.task_type: str = self.TASK_TYPE
        self.action_interval: int = self.ACTION_INTERVAL
        self.verbose: bool = self.DEFAULT_VERBOSE
        self._load_config()

    def _load_config(self) -> None:
        """从环境变量加载配置"""
        push_key_env = os.environ.get(self.ENV_PUSH_KEY)
        serverchan_key_env = os.environ.get(self.ENV_SERVERCHAN_KEY)
        raw_cookies_env = os.environ.get(self.ENV_COOKIES)
        verbose_env = os.environ.get(self.ENV_VERBOSE)

        # 推送密钥
        self.push_key = push_key_env or ""
        self.serverchan_key = serverchan_key_env or ""
        if not push_key_env:
            logger.warning(f"{LogEmoji.WARNING} 环境变量 '{self.ENV_PUSH_KEY}' 未设置。")
        if not serverchan_key_env:
            logger.warning(f"{LogEmoji.WARNING} 环境变量 '{self.ENV_SERVERCHAN_KEY}' 未设置。")

        # Cookie：多账号用 '&' 分隔。cookie 值本身含 ';'、'='、'%'，'&' 冲突概率最低
        if not raw_cookies_env:
            logger.warning(f"{LogEmoji.WARNING} 环境变量 '{self.ENV_COOKIES}' 未设置。")
            self.cookies_list = []
        else:
            self.cookies_list = [cookie.strip() for cookie in raw_cookies_env.split("&") if cookie.strip()]
            if not self.cookies_list:
                raise ValueError(f"环境变量 '{self.ENV_COOKIES}' 已设置，但未包含任何有效的 Cookie。")

        # 详细日志
        if verbose_env is not None:
            verbose_lower = verbose_env.lower()
            if verbose_lower in ("true", "1", "yes", "y"):
                self.verbose = True
            elif verbose_lower in ("false", "0", "no", "n"):
                self.verbose = False
            else:
                logger.warning(f"{LogEmoji.WARNING} 环境变量 '{self.ENV_VERBOSE}' 的值 '{verbose_env}' 无效，使用默认值 {self.DEFAULT_VERBOSE}。")

        logger.info(f"{LogEmoji.INFO} 站点: {self.DOMAIN}")
        logger.info(f"{LogEmoji.INFO} 共加载了 {len(self.cookies_list)} 个 Cookie。")
        logger.info(f"{LogEmoji.INFO} 动作顺序: {'签到, ' if self.sign_enabled else ''}{', '.join(self.tasks)}，间隔 {self.action_interval} 秒")
        logger.info(f"{LogEmoji.INFO} {self.ENV_PUSH_KEY} {'已设置' if push_key_env else '未设置'}，{self.ENV_SERVERCHAN_KEY} {'已设置' if serverchan_key_env else '未设置'}。")
        logger.info(f"{LogEmoji.INFO} 当前 {self.ENV_VERBOSE}: {self.verbose}。")


class API:
    """站点 API 调用"""

    SIGN_URL = Endpoint.SIGN.value
    TASK_URL = Endpoint.TASK.value

    """成功返回码（该站点 1 = 成功，与 GLaDOS 的 0 不同）"""
    SUCCESS_CODES = (1,)

    """登录态失效关键词"""
    AUTH_KEYWORDS = ("登录", "登陆", "无权限", "未授权", "权限不足")

    """请求被拒关键词（站点对无效会话返回"非法请求！(1700001)"，而非"请先登录"）"""
    REJECTED_KEYWORDS = ("非法请求", "非法操作", "请求异常")

    """重复领取关键词"""
    REPEAT_KEYWORDS = ("已领取", "已领", "已完成", "重复", "领取过", "已经领")

    """重复签到关键词"""
    SIGNED_KEYWORDS = ("已签到", "已经签到", "今日已签")

    """请求协议（默认 https，本地联调时可改为 http）"""
    SCHEME = "https"

    USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36"

    def __init__(self, cookie_raw: str, cookie_index: int = 0, verbose: bool = False, domain: Optional[str] = None):
        self.domain: str = domain or Config.DOMAIN
        self.cookie_index: int = cookie_index
        self.verbose: bool = verbose
        self.jar = parse_cookie_header(cookie_raw)
        self.session = requests.Session()
        self.session.headers.update(self._base_headers())
        # 只对 GET 自动重试：领取奖励的 POST 若被重发可能造成重复请求，
        # 网络抖动交给下一次定时任务兜底，比冒险重发更稳。
        retry_strategy = Retry(
            total=3,
            backoff_factor=1.0,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=("GET",),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    def __del__(self):
        self.close()

    def close(self) -> None:
        if hasattr(self, "session"):
            try:
                self.session.close()
            except Exception as e:
                logger.error(f"{LogEmoji.ERROR} 关闭 session 时发生错误: {e}")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False

    def _log(self, level: str, emoji: str, message: str, force: bool = False) -> None:
        """统一日志输出方法"""
        prefix = f"{LogEmoji.COOKIE}[{self.cookie_index}] {LogEmoji.SITE}[{self.domain}]"
        log_message = f"{prefix} {emoji} {message}"
        if force or self.verbose:
            if level == "info":
                logger.info(log_message)
            elif level == "warning":
                logger.warning(log_message)
            elif level == "error":
                logger.error(log_message)

    def _base_headers(self) -> Dict[str, str]:
        """构建基础请求头（与抓包保持一致，Origin/Referer/X-Requested-With 是站点的同源校验所需）"""
        return {
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "User-Agent": self.USER_AGENT,
            "Origin": f"{self.SCHEME}://{self.domain}",
            "Referer": f"{self.SCHEME}://{self.domain}/",
            "DNT": "1",
            "Sec-CH-UA": '"Not A(Brand";v="8", "Chromium";v="132", "Google Chrome";v="132"',
            "Sec-CH-UA-Mobile": "?0",
            "Sec-CH-UA-Platform": '"Windows"',
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        }

    def _cookie_header(self) -> str:
        """把 cookie 字典拼回请求头。手工拼接而非交给 requests，避免它对 %7C 之类做二次编码"""
        return "; ".join(f"{name}={value}" for name, value in self.jar.items())

    def _absorb_cookies(self, response: requests.Response) -> None:
        """吸收服务端 Set-Cookie（主要是 PHPSESSID），保证同账号后续请求会话一致"""
        try:
            for cookie in response.cookies:
                self.jar[cookie.name] = cookie.value
        except Exception:
            pass

    def _make_request(self, path: str, method: str = "GET", data: Optional[Dict] = None) -> Optional[requests.Response]:
        """发送 HTTP 请求"""
        url = f"{self.SCHEME}://{self.domain}{path}"
        headers = self._base_headers()
        headers["Cookie"] = self._cookie_header()
        if method.upper() == "POST":
            headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
            headers["X-Requested-With"] = "XMLHttpRequest"

        try:
            if method.upper() == "POST":
                response = self.session.post(url, headers=headers, data=data, timeout=(15, 30))
            elif method.upper() == "GET":
                response = self.session.get(url, headers=headers, timeout=(15, 30))
            else:
                self._log("error", LogEmoji.ERROR, f"不支持的 HTTP 方法: {method}", force=True)
                return None

            self._absorb_cookies(response)

            if not response.ok:
                self._log("warning", LogEmoji.WARNING, f"向 {url} 发起的请求失败，状态码 {response.status_code}。响应内容: {response.text[:200]}", force=True)
                return None
            return response
        except requests.exceptions.RequestException as e:
            self._log("error", LogEmoji.ERROR, f"向 {url} 发起请求时发生网络错误: {e}", force=True)
            return None

    @log_method
    def warmup(self) -> bool:
        """访问首页建立会话，并前置校验登录态是否有效

        返回 False 表示该账号本轮无需继续发请求（网络不通或 Cookie 里没有登录态）。
        """
        response = self._make_request(Endpoint.HOME.value, "GET")
        if response is None:
            self._log("error", LogEmoji.ERROR, "首页不可达，跳过该账号", force=True)
            return False

        has_login_cookie = any(name.startswith("wordpress_logged_in_") for name in self.jar)
        if not has_login_cookie:
            self._log("error", LogEmoji.ERROR, "Cookie 中缺少 wordpress_logged_in_*，登录态无效", force=True)
            return False

        self._log("info", LogEmoji.SUCCESS, f"会话就绪（cookie 字段 {len(self.jar)} 个）")
        return True

    @log_method
    def sign(self) -> Dict[str, object]:
        """执行每日签到（sign.php）"""
        data = {"sign": "1", "ticket": "", "randstr": ""}
        response = self._make_request(self.SIGN_URL, "POST", data)
        if response is None:
            return self._fail_result("", "网络请求失败", "签到失败")

        raw = self._load_json(response, "签到")
        if raw is None:
            return self._fail_result("", "响应解析失败", "签到失败")
        return self._parse_sign(raw)

    @log_method
    def claim_task(self, task_id: str, task_type: str = "day") -> Dict[str, object]:
        """领取单个任务奖励"""
        data = {"task_id": task_id, "type": task_type}
        response = self._make_request(self.TASK_URL, "POST", data)
        if response is None:
            return self._fail_result(task_id, "网络请求失败")

        raw = self._load_json(response, task_id)
        if raw is None:
            return self._fail_result(task_id, "响应解析失败")
        return self._parse_claim(task_id, raw)

    def _load_json(self, response: requests.Response, tag: str) -> Optional[Dict]:
        """解析 JSON 响应，失败返回 None"""
        try:
            raw = response.json()
        except ValueError:
            self._log("error", LogEmoji.ERROR, f"{tag} 响应非 JSON: {response.text[:120]}", force=True)
            return None
        if not isinstance(raw, dict):
            self._log("error", LogEmoji.ERROR, f"{tag} 响应格式异常: {type(raw).__name__}", force=True)
            return None
        return raw

    def _result(self, name: str, status: str, message: str, code: ClaimStatus, auth_error: bool = False) -> Dict[str, object]:
        """构造统一结果字典"""
        return {
            "name": name,
            "status": status,
            "message": message,
            "code": code,
            "auth_error": auth_error,
        }

    def _fail_result(self, name: str, message: str, status: str = "领取失败") -> Dict[str, object]:
        """构造失败结果"""
        return self._result(name, status, message, ClaimStatus.FAILURE)

    def _failure_status(self, message: str) -> Optional[Tuple[str, bool]]:
        """按关键词判定会话级失败：返回 (状态, 是否应中断该账号)"""
        if any(keyword in message for keyword in self.AUTH_KEYWORDS):
            return ("登录失效", True)
        if any(keyword in message for keyword in self.REJECTED_KEYWORDS):
            return ("请求被拒", True)
        return None

    def _short_message(self, status: str, message: str, code: ClaimStatus) -> str:
        """推送只保留最短说明，避免整句站点文案刷屏"""
        if code == ClaimStatus.SUCCESS:
            return ""
        if code == ClaimStatus.REPEAT:
            return "已签到" if status == "已签到" else "已领取"
        if status in ("登录失效", "请求被拒"):
            return status
        if "未完成" in message or "没有完成" in message:
            return "任务未完成"
        return message.strip(" ！!。.·")

    def _parse_sign(self, raw: Dict) -> Dict[str, object]:
        """解析签到响应

        响应示例：{"code":1,"msg":"签到成功","sign_c":45,"text":"今日已签到",...}
        sign_c 是累计签到天数，只写日志，不进推送。
        """
        code = raw.get("code")
        message = str(raw.get("msg") or raw.get("text") or "无消息字段")
        days = raw.get("sign_c")

        self._log("info", LogEmoji.INFO, f"签到 原始响应: {raw}")

        if code in self.SUCCESS_CODES:
            extra = f"，累计 {days} 天" if days not in (None, "") else ""
            self._log("info", LogEmoji.SUCCESS, f"签到成功{extra}")
            return self._result("", "签到成功", "", ClaimStatus.SUCCESS)

        failure = self._failure_status(message)
        if failure:
            status, _ = failure
            hint = (
                f" —— 请重新登录 {self.domain} 刷新 Cookie 并更新 Secret"
                if status == "登录失效"
                else f" —— Cookie 可能已失效或不完整，请完整复制 {self.domain} 的 Cookie"
            )
            self._log("error", LogEmoji.ERROR, f"签到{status}(code={code})：{message}{hint}", force=True)
            return self._result("", status, "", ClaimStatus.FAILURE, True)

        if any(keyword in message for keyword in self.SIGNED_KEYWORDS):
            self._log("info", LogEmoji.REPEAT, message, force=True)
            return self._result("", "已签到", "", ClaimStatus.REPEAT)

        self._log("warning", LogEmoji.WARNING, f"签到失败(code={code})：{message}", force=True)
        return self._result("", "签到失败", self._short_message("签到失败", message, ClaimStatus.FAILURE), ClaimStatus.FAILURE)

    def _parse_claim(self, task_id: str, raw: Dict) -> Dict[str, object]:
        """解析领取响应

        响应示例：{"code":1,"msg":"奖励领取成功！","task":35}
        注意：task 字段不是奖励额度，不参与任何展示。
        """
        code = raw.get("code")
        message = str(raw.get("msg") or raw.get("message") or "无消息字段")

        # 详细模式下打印原始响应，便于排查接口字段变化
        self._log("info", LogEmoji.INFO, f"{task_id} 原始响应: {raw}")

        if code in self.SUCCESS_CODES:
            self._log("info", LogEmoji.SUCCESS, f"{task_id} 领取成功 · {message}")
            return self._result(task_id, "领取成功", "", ClaimStatus.SUCCESS)

        failure = self._failure_status(message)
        if failure:
            status, _ = failure
            hint = (
                f" —— 请重新登录 {self.domain} 刷新 Cookie 并更新 Secret"
                if status == "登录失效"
                else f" —— Cookie 可能已失效或不完整，请完整复制 {self.domain} 的 Cookie（需含 wordpress_logged_in_* 与会话字段）"
            )
            self._log("error", LogEmoji.ERROR, f"{task_id} {status}(code={code})：{message}{hint}", force=True)
            return self._result(task_id, status, "", ClaimStatus.FAILURE, True)

        if any(keyword in message for keyword in self.REPEAT_KEYWORDS):
            self._log("info", LogEmoji.REPEAT, f"{task_id} 今日已领取 · {message}", force=True)
            return self._result(task_id, "重复领取", "已领取", ClaimStatus.REPEAT)

        self._log("warning", LogEmoji.WARNING, f"{task_id} 领取失败(code={code})：{message}", force=True)
        return self._result(task_id, "领取失败", self._short_message("领取失败", message, ClaimStatus.FAILURE), ClaimStatus.FAILURE)


@dataclass()
class ActionResult:
    """单个动作（签到 / 领取任务）的结果"""

    cookie_index: int
    name: str = ""
    status: str = "失败"
    message: str = ""
    code: ClaimStatus = ClaimStatus.FAILURE

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


def render_payload(payload: dict, line_sep: str = "\n") -> str:
    """将结构化结果渲染为文本。line_sep 控制行分隔：
    PushDeer 用 \\n（原生换行），Server酱 用 '  \\n'（Markdown 硬换行，避免段间空行）。"""
    lines = []
    for row in payload.get("rows", []):
        parts = [f"#{row['index']}", row["emoji"], row["status"]]
        if row.get("name"):
            parts.append(row["name"])
        line = "  ".join(parts)
        if row.get("message"):
            line += f"  · {row['message']}"
        lines.append(line)
    note = payload.get("note")
    if note:
        lines.append(note)
    return line_sep.join(lines)


class PushBackend:
    """推送后端抽象基类（策略模式）"""

    name: str = "base"

    def send(self, title: str, payload: dict) -> bool:
        raise NotImplementedError

    def _render(self, payload: dict) -> str:
        return render_payload(payload, "\n")


class PushDeerBackend(PushBackend):
    """PushDeer 后端"""

    name = "PushDeer"

    def __init__(self, push_key: str):
        self.push_key = push_key

    def send(self, title: str, payload: dict) -> bool:
        # 延迟导入：未安装 SDK 时不阻断其他后端
        try:
            from pypushdeer import PushDeer
        except ImportError:
            logger.error(f"{LogEmoji.ERROR} [{self.name}] 推送失败: 未安装 pypushdeer，请先执行 pip install pypushdeer。")
            return False
        try:
            PushDeer(pushkey=self.push_key).send_text(title, desp=self._render(payload))
            return True
        except Exception as e:
            logger.error(f"{LogEmoji.ERROR} [{self.name}] 推送失败: {e}")
            return False


class ServerChanBackend(PushBackend):
    """Server酱 后端"""

    name = "Server酱"

    """消息已投递、但服务端记账失败的错误码

    30001 = Server酱 写入 wxstatus 字段时的 DB 异常。实测该错误发生在消息推送
    之后，用户能正常收到，只是接口返回了错误码。若按失败处理会导致重复推送。
    """
    DELIVERED_ERROR_CODES = ("30001",)

    def __init__(self, send_key: str):
        self.send_key = send_key

    def _render(self, payload: dict) -> str:
        # Server酱 按 Markdown 渲染，单 \n 不换行；用行尾两空格+\n 做硬换行
        return render_payload(payload, "  \n")

    def send(self, title: str, payload: dict) -> bool:
        try:
            from serverchan_sdk import sc_send
        except ImportError:
            logger.error(f"{LogEmoji.ERROR} [{self.name}] 推送失败: 未安装 serverchan_sdk，请先执行 pip install serverchan-sdk。")
            return False

        try:
            resp = sc_send(sendkey=self.send_key, title=title, desp=self._render(payload))
        except Exception as e:
            logger.error(f"{LogEmoji.ERROR} [{self.name}] 推送失败: {e}")
            return False

        if not isinstance(resp, dict):
            logger.error(f"{LogEmoji.ERROR} [{self.name}] 推送失败: 响应格式异常 {resp}")
            return False

        code = str(resp.get("code"))
        if code == "0":
            return True

        # 消息已送达但服务端记账失败：按成功处理，绝不重发（否则用户会收到两条）
        if code in self.DELIVERED_ERROR_CODES:
            logger.warning(
                f"{LogEmoji.WARNING} [{self.name}] 接口返回 {code}（服务端记账异常），消息应已送达，不重发: {resp.get('message', '')}"
            )
            return True

        logger.error(f"{LogEmoji.ERROR} [{self.name}] 推送失败: 接口返回 {resp}")
        return False
        logger.error(f"{LogEmoji.ERROR} [{self.name}] 推送失败: 接口返回 {resp}")
        return False


class PushService:
    """推送服务：聚合多个后端，向全部已配置后端发送（都发）"""

    def __init__(self, config: Optional[Config]):
        self.backends: List[PushBackend] = []
        if config:
            if getattr(config, "push_key", ""):
                self.backends.append(PushDeerBackend(config.push_key))
            if getattr(config, "serverchan_key", ""):
                self.backends.append(ServerChanBackend(config.serverchan_key))

    def send(self, title: str, payload: dict) -> bool:
        if not self.backends:
            logger.info(f"{LogEmoji.WARNING} 未配置推送密钥，跳过。")
            return False

        backend_names = "、".join(b.name for b in self.backends)
        logger.info(f"{LogEmoji.INFO} 已配置 {len(self.backends)} 个后端（{backend_names}），开始发送")
        results = [backend.send(title, payload) for backend in self.backends]
        if any(results):
            logger.info(f"{LogEmoji.SUCCESS} 发送完成：{sum(results)}/{len(results)} 个后端成功")
            return True
        logger.error(f"{LogEmoji.ERROR} 全部后端发送失败")
        return False


class Checker:
    """任务领取调度"""

    def __init__(self, config: Config):
        self.config = config
        self.results: List[ClaimResult] = []

    def _log(self, cookie_idx: int, task: str, emoji: str, message: str, force: bool = False) -> None:
        if self.config.verbose or force:
            logger.info(f"{LogEmoji.COOKIE}[{cookie_idx}] {LogEmoji.TASK}[{task}] {emoji} {message}")

    def checkin_all(self) -> None:
        """遍历所有账号，按顺序执行签到与任务领取"""
        cookie_count = len(self.config.cookies_list)
        actions = self._actions()
        logger.info(f"{LogEmoji.INFO} 共 {cookie_count} 个账号, {len(actions)} 个动作")

        for cookie_idx, cookie in enumerate(self.config.cookies_list, 1):
            logger.info(f"{LogEmoji.START} ========== 开始处理账号 {cookie_idx} ==========")

            with API(cookie, cookie_idx, verbose=self.config.verbose) as api:
                if not api.warmup():
                    self.results.append(
                        ActionResult(cookie_idx, "整账号", status="已跳过", message="登录态无效")
                    )
                    self._log(cookie_idx, "整账号", LogEmoji.SKIP, "登录态无效，整账号跳过", force=True)
                    continue

                for action_index, (kind, key) in enumerate(actions, 1):
                    label = "签到" if kind == "sign" else key
                    emoji = LogEmoji.SIGN if kind == "sign" else LogEmoji.TASK
                    logger.info(f"{LogEmoji.INFO} ----- 账号 {cookie_idx} · {emoji}[{label}] -----")

                    raw = api.sign() if kind == "sign" else api.claim_task(key, self.config.task_type)
                    result = ActionResult(
                        cookie_index=cookie_idx,
                        name=str(raw["name"]),
                        status=str(raw["status"]),
                        message=str(raw["message"]),
                        code=raw["code"],
                    )
                    self.results.append(result)
                    self._report(result)

                    # 登录态一旦失效，后续动作不必再打请求
                    if raw.get("auth_error"):
                        self._log(cookie_idx, label, LogEmoji.SKIP, "登录态失效，跳过该账号剩余动作", force=True)
                        break

                    # 每个动作之间间隔若干秒，避免短时间连发请求
                    if action_index < len(actions) and self.config.action_interval > 0:
                        self._log(
                            cookie_idx,
                            label,
                            LogEmoji.WAIT,
                            f"间隔 {self.config.action_interval} 秒后继续",
                            force=True,
                        )
                        time.sleep(self.config.action_interval)

    def _actions(self) -> List[Tuple[str, str]]:
        """按顺序返回待执行动作：签到 → 各任务"""
        actions: List[Tuple[str, str]] = []
        if self.config.sign_enabled:
            actions.append(("sign", "签到"))
        actions.extend(("task", task_id) for task_id in self.config.tasks)
        return actions

    def _report(self, result: ActionResult) -> None:
        label = result.name or "签到"
        text = f"{result.status} · {result.message}" if result.message else result.status
        if result.code == ClaimStatus.SUCCESS:
            self._log(result.cookie_index, label, LogEmoji.SUCCESS, text, force=True)
        elif result.code == ClaimStatus.REPEAT:
            self._log(result.cookie_index, label, LogEmoji.REPEAT, text, force=True)
        else:
            self._log(result.cookie_index, label, LogEmoji.WARNING, text, force=True)

    def get_results(self) -> List[Dict[str, object]]:
        return [result.to_dict() for result in self.results]

    def format_results(self) -> dict:
        """格式化为结构化 payload（表现交给各推送后端/日志各自渲染）"""
        results = self.get_results()

        success_count = sum(1 for r in results if r["code"] == ClaimStatus.SUCCESS)
        repeat_count = sum(1 for r in results if r["code"] == ClaimStatus.REPEAT)
        fail_count = sum(1 for r in results if r["code"] == ClaimStatus.FAILURE)

        status_emoji_map = {
            ClaimStatus.SUCCESS: LogEmoji.SUCCESS,
            ClaimStatus.REPEAT: LogEmoji.REPEAT,
            ClaimStatus.FAILURE: LogEmoji.ERROR,
        }
        title = f"7risha 签到 · 成功 {success_count} · 重复 {repeat_count} · 失败 {fail_count}"

        rows = []
        for i, res in enumerate(results, 1):
            rows.append({
                "index": i,
                "emoji": status_emoji_map.get(res["code"], LogEmoji.ERROR),
                "status": res["status"],
                "name": res["name"],
                "message": res["message"],
                "code": res["code"],
            })

        return {
            "title": title,
            "rows": rows,
            "success": success_count,
            "fail": fail_count,
            "repeat": repeat_count,
        }


logger = init_logger()


def main():
    """主函数"""
    exit_code = 0
    config: Optional[Config] = None
    try:
        logger.info(f"{LogEmoji.START} 步骤 1: 加载配置")
        config = Config()

        if not config.cookies_list:
            logger.error(f"{LogEmoji.ERROR} 未找到有效的 Cookie, 退出程序。")
            payload = {"title": "7risha 签到 · 未找到 Cookie", "rows": []}
        else:
            logger.info(f"{LogEmoji.START} 步骤 2: 领取任务奖励")
            checker = Checker(config)
            checker.checkin_all()

            logger.info(f"{LogEmoji.START} 步骤 3: 格式化结果")
            payload = checker.format_results()
            logger.info(f"\n{LogEmoji.END} ========== 签到总结 ==========\n{payload['title']}\n{render_payload(payload)}")

    except Exception as e:
        logger.error(f"{LogEmoji.ERROR} 主程序执行过程中发生未预期的错误: {e}")
        payload = {"title": "7risha 签到 · 脚本执行出错", "rows": [], "note": str(e)}
        exit_code = 1

    logger.info(f"{LogEmoji.START} 步骤 4: 发送推送")
    PushService(config).send(payload["title"], payload)
    logger.info(f"{LogEmoji.END} 签到完成")

    if exit_code != 0:
        sys.exit(exit_code)


if __name__ == "__main__":
    main()
