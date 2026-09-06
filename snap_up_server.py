"""三个地域独立重试；导入此模块不会读取 Cookie 或发送请求。"""
import json
import math
import os
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import requests
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, sync_playwright


BEIJING = timezone(timedelta(hours=8))
COOKIE_FILE = Path(__file__).resolve().parent / "cookies.json"
CONFIG_FILE = Path(__file__).resolve().parent / "config.json"
DEFAULT_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
DAILY_RUSH_HOURS = (10, 15)
REGION_IDS = [1, 4, 8]
REGION_NAMES = {1: "广州", 4: "上海", 8: "北京"}
ACTIVITY_ID = 164461404341040
ACT_ID = 1897632168296710
LOGIN_URL = "https://cloud.tencent.com/login"
ACTIVITY_URL = os.environ.get("TENCENT_ACTIVITY_URL", "https://cloud.tencent.com/act/pro/featured-202607?fromSource=gwzcw.11914902.11914902.11914902&utm_medium=cpc&utm_id=gwzcw.11914902.11914902.11914902&gad_source=1&gad_campaignid=1480177331&gbraid=0AAAAADDDx-rm1Uu3nfP4-7MwQ9R7lPkOL&gclid=Cj0KCQjw--7UBhCpARIsAGJBptiLarwO5_Xu1C_pjWIsMEwGyWgvUb-LFyYzA37Xif4EfLcbHUhPXqEaAoQGEALw_wcB")
CHECK_URL = "https://act-api.cloud.tencent.com/dianshi/check-available"
BUY_URL = "https://act-api.cloud.tencent.com/dianshi/do-goods"
LOG_LOCK = threading.Lock()


@dataclass(frozen=True)
class Settings:
    max_attempts: int = 60          # 次数和 30 秒窗口双重限制
    retry_base_ms: int = 100        # 兼容旧配置，主循环改用 send_interval_ms
    retry_max_ms: int = 300         # 兼容旧配置，主循环改用并行通道补发
    send_interval_ms: int = 20      # 响应未返回时，下一条在途请求的最小发出间隔
    parallel_requests_per_region: int = 3  # 每地域独立请求通道数
    connect_timeout_ms: int = 3000
    read_timeout_ms: int = 5000
    window_seconds: float = 30     # 从开抢时间起算，到期停止新提交
    warmup_seconds: float = 5
    rate_limit_fallback: float = 1  # 仅用于未提供 Retry-After 的限流响应

    def __post_init__(self):
        if not isinstance(self.max_attempts, int) or self.max_attempts < 1:
            raise ValueError("max_attempts 必须是正整数")
        for name in ("retry_base_ms", "retry_max_ms", "send_interval_ms", "connect_timeout_ms",
                     "read_timeout_ms", "window_seconds", "warmup_seconds", "rate_limit_fallback"):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} 必须是有限正数")
        if self.retry_max_ms < self.retry_base_ms:
            raise ValueError("retry_max_ms 不能小于 retry_base_ms")
        if (not isinstance(self.parallel_requests_per_region, int) or
                self.parallel_requests_per_region < 1):
            raise ValueError("parallel_requests_per_region 必须是正整数")

    @property
    def timeout(self):
        return self.connect_timeout_ms / 1000, self.read_timeout_ms / 1000


@dataclass
class Outcome:
    kind: str
    message: str = ""
    code: object = None
    http_status: object = None
    retry_after: float = 0
    payload: object = None


# 未确认腾讯云业务错误码，不编造数字映射。仅重试有明确失败信息的响应。
FATAL_WORDS = ("csrf", "token", "登录", "login", "权限", "资格", "实名",
               "限购", "已购买", "参数", "验证码", "风控", "denied")
RATE_WORDS = ("频繁", "限流", "频率过快", "访问频率", "too many requests", "rate limit")
RETRY_WORDS = ("未开始", "尚未开始", "库存不足", "无库存", "暂无库存",
               "售罄", "抢光", "服务器繁忙", "服务繁忙", "系统繁忙",
               "下单人数过多", "拥挤", "稍后重试", "稍后再试",
               "out of stock", "sold out", "not started", "server busy")


def log(message):
    with LOG_LOCK:
        stamp = datetime.now(BEIJING).isoformat(timespec="milliseconds")
        print(f"[{stamp}] {message}", flush=True)


def is_activity_url(value):
    try:
        parsed = urlsplit(value)
    except (TypeError, ValueError):
        return False
    return (parsed.scheme == "https" and
            parsed.hostname in ("cloud.tencent.com", "www.cloud.tencent.com") and
            parsed.path.startswith("/act/"))


def build_login_url(activity_url):
    """保留活动回跳地址，确保扫码登录完成后回到当前活动页。"""
    return f"{LOGIN_URL}?{urlencode({'s_url': activity_url})}"


def validate_cookies(cookies):
    if not isinstance(cookies, list) or not cookies:
        raise ValueError("Cookie 列表不能为空，请重新获取登录态")
    for cookie in cookies:
        if (not isinstance(cookie, dict) or not cookie.get("name") or
                not isinstance(cookie.get("value"), str)):
            raise ValueError("Cookie 列表中存在无效字段")
    if not any(c["name"] in ("skey", "p_skey") and c["value"] and
               (c.get("expires", -1) == -1 or c.get("expires", 0) > time.time())
               for c in cookies):
        raise ValueError("缺少未过期的登录 Cookie，请重新扫码登录")
    return cookies


def load_cookies(path=COOKIE_FILE):
    with open(path, encoding="utf-8") as file:
        return validate_cookies(json.load(file))


def save_config(csrf_token, activity_url, path=CONFIG_FILE):
    """仅更新本地活动配置，Cookie 和 CSRF 仍分别保存。"""
    config = {}
    if path.exists():
        try:
            with open(path, encoding="utf-8") as file:
                config = json.load(file)
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"无法读取现有配置：{exc}") from exc
        if not isinstance(config, dict):
            raise ValueError("config.json 必须是 JSON 对象")
    config["csrf_token"] = csrf_token
    config["activity_url"] = activity_url
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def load_config(path=CONFIG_FILE):
    with open(path, encoding="utf-8") as file:
        config = json.load(file)
    if not isinstance(config, dict):
        raise ValueError("config.json 必须是 JSON 对象")
    token = config.get("csrf_token") or config.get("CSRF_TOKEN")
    if not isinstance(token, str) or not token.strip():
        raise ValueError("config.json 缺少 csrf_token，请手动填写实测有效值")
    activity_url = config.get("activity_url", ACTIVITY_URL)
    if not isinstance(activity_url, str) or not activity_url.startswith("https://cloud.tencent.com/act/"):
        raise ValueError("config.json 的 activity_url 必须是腾讯云活动页地址")
    return {"csrf_token": token.strip(), "activity_url": activity_url}


def load_activity_url(path=CONFIG_FILE):
    """读取活动地址；本轮 CSRF 不依赖配置文件，而由浏览器登录后获取。"""
    if not path.exists():
        return ACTIVITY_URL
    try:
        with open(path, encoding="utf-8") as file:
            config = json.load(file)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取活动配置：{exc}") from exc
    activity_url = config.get("activity_url", ACTIVITY_URL) if isinstance(config, dict) else ACTIVITY_URL
    if not isinstance(activity_url, str) or not is_activity_url(activity_url):
        raise ValueError("活动地址必须是 https://cloud.tencent.com/act/... ")
    return activity_url


def _capture_check_token(captured, request):
    url = request.url.lower()
    if "act-api.cloud.tencent.com" not in url or "/dianshi/" not in url:
        return
    path = urlsplit(request.url).path.lower()
    if "check" not in path or ("goods" not in path and "available" not in path):
        return
    token = request.all_headers().get("x-csrf-token", "").strip()
    if token and token not in ("0", "null", "undefined"):
        captured["token"] = token


def _save_browser_credentials(credentials):
    """将本轮浏览器结果分别落盘，同时主程序继续使用内存中的结果。"""
    save_config(credentials["csrf_token"], credentials["activity_url"], CONFIG_FILE)
    temporary = COOKIE_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(credentials["cookies"], ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, COOKIE_FILE)


def _wait_for_browser_token(page, captured, timeout_seconds=90):
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline and not captured.get("token"):
        page.wait_for_timeout(250)
    if not captured.get("token"):
        raise ValueError("未捕获登录后的有效 check_goods CSRF，请确认登录成功并刷新活动页")


def start_browser_credentials(activity_url):
    """启动并保持浏览器，登录后获取本轮 Cookie/CSRF。"""
    playwright = sync_playwright().start()
    browser = None
    try:
        browser = playwright.chromium.launch(headless=False)
        context = browser.new_context()
        try:
            context.grant_permissions(["local-network-access"], origin="https://cloud.tencent.com")
            log("已为腾讯云活动页启用本地网络访问权限。")
        except Exception as exc:
            log(f"本地网络权限未能自动授予（{type(exc).__name__}），可使用二维码登录。")
        context.route("https://act-api.cloud.tencent.com/dianshi/do-goods**",
                      lambda route: route.abort())
        page = context.new_page()
        captured = {}
        context.on("request", lambda request: _capture_check_token(captured, request))
        try:
            page.goto(build_login_url(activity_url), wait_until="domcontentloaded", timeout=60000)
        except PlaywrightTimeoutError:
            log("登录页加载超时，但浏览器仍会保留，请继续操作。")
        log("请扫码登录；程序会等待登录跳转，浏览器保持运行。")
        try:
            page.wait_for_url("**/act/**", timeout=120000)
        except PlaywrightTimeoutError:
            input("未自动跳转，请在浏览器打开当前有效活动页后按回车：")
        if not is_activity_url(page.url):
            raise ValueError("当前页面不是腾讯云活动页")
        captured.clear()  # 丢弃登录前的 0 或旧 token。
        try:
            page.reload(wait_until="domcontentloaded", timeout=60000)
        except PlaywrightTimeoutError:
            log("活动页刷新超时，继续等待登录后的接口请求。")
        _wait_for_browser_token(page, captured)
        credentials = {
            "cookies": validate_cookies(context.cookies()),
            "csrf_token": captured["token"],
            "activity_url": page.url,
            "user_agent": page.evaluate("navigator.userAgent"),
        }
        credentials["csrf_provider"] = lambda: captured.get("token", credentials["csrf_token"])
        _save_browser_credentials(credentials)
        log(f"浏览器登录凭据已获取：CSRF={credentials['csrf_token']}；Cookie数量={len(credentials['cookies'])}")
        return {"playwright": playwright, "browser": browser, "context": context,
                "page": page, "captured": captured, "credentials": credentials}
    except Exception:
        if browser is not None:
            browser.close()
        playwright.stop()
        raise


def refresh_browser_credentials(browser_session):
    """本轮抢购无成功结果时，在同一浏览器中重新刷新并获取凭据。"""
    page = browser_session["page"]
    captured = browser_session["captured"]
    captured.clear()
    try:
        page.reload(wait_until="domcontentloaded", timeout=60000)
    except PlaywrightTimeoutError:
        log("失败后的活动页刷新超时，继续等待新的 CSRF。")
    _wait_for_browser_token(page, captured)
    credentials = browser_session["credentials"]
    credentials["cookies"] = validate_cookies(browser_session["context"].cookies())
    credentials["csrf_token"] = captured["token"]
    credentials["activity_url"] = page.url
    _save_browser_credentials(credentials)
    log(f"失败后已重新获取凭据：CSRF={credentials['csrf_token']}；Cookie数量={len(credentials['cookies'])}")
    return credentials


def load_credentials(config_path=CONFIG_FILE, cookie_path=COOKIE_FILE):
    config = load_config(config_path)
    return {
        "cookies": load_cookies(cookie_path),
        "csrf_token": config["csrf_token"],
        "activity_url": config["activity_url"],
        "user_agent": config.get("user_agent", DEFAULT_USER_AGENT),
    }


def create_session(auth):
    if not isinstance(auth, dict) or not isinstance(auth.get("csrf_token"), str):
        raise ValueError("缺少配置文件中的 csrf_token")
    validate_cookies(auth["cookies"])
    cookies = auth["cookies"]
    activity_url = auth.get("activity_url", ACTIVITY_URL)
    client = requests.Session()
    client.headers.update({
        "x-csrf-token": auth["csrf_token"],
        "Content-Type": "application/json",
        "User-Agent": auth.get("user_agent", DEFAULT_USER_AGENT),
        "Origin": "https://cloud.tencent.com",
        "X-Requested-With": "XMLHttpRequest",
        "referer": activity_url,
    })
    # 所有重试由业务循环控制，避免 HTTP 层偷偷增加 POST 次数。
    client.mount("https://", requests.adapters.HTTPAdapter(
        max_retries=0, pool_connections=1, pool_maxsize=1))
    for cookie in cookies:
        options = {"path": cookie.get("path", "/"), "secure": cookie.get("secure", False)}
        if cookie.get("domain"):
            options["domain"] = cookie["domain"]
        if cookie.get("expires", -1) > 0:
            options["expires"] = int(cookie["expires"])
        client.cookies.set(cookie["name"], cookie["value"], **options)
    client._csrf_provider = auth.get("csrf_provider")
    return client


def retry_after_seconds(value, fallback):
    if value is None:
        return fallback
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        try:
            seconds = parsedate_to_datetime(value).timestamp() - time.time()
        except (TypeError, ValueError, OverflowError):
            return fallback
    return max(0, seconds) if math.isfinite(seconds) else fallback


def classify_response(response, settings):
    status = response.status_code
    if status == 429:
        return Outcome("rate_limit", "服务端限流", http_status=status,
                       retry_after=retry_after_seconds(response.headers.get("Retry-After"), settings.rate_limit_fallback))
    try:
        data = response.json()
    except ValueError:
        if 400 <= status < 500:
            return Outcome("fatal", "HTTP 请求被拒绝，响应非 JSON", http_status=status)
        return Outcome("retry", "响应不是有效 JSON，窗口内继续尝试", http_status=status)
    if status in (401, 403) or 300 <= status < 400:
        message = str(data.get("msg") or data.get("message") or "鉴权失败或重定向") if isinstance(data, dict) else "鉴权失败或重定向"
        return Outcome("fatal", message, http_status=status)
    if not isinstance(data, dict) or "code" not in data:
        return Outcome("retry", "响应缺少业务 code，窗口内继续尝试", http_status=status)
    code = data["code"]
    # bool 也是 int 的子类，不能把 code:false 当成 code:0。
    if 200 <= status < 300 and (type(code) is int and code == 0 or code == "0"):
        return Outcome("success", "接口报告成功", code, status, payload=data)
    message = str(data.get("msg") or data.get("message") or "未知业务错误")
    text = message.lower()
    if any(word in text for word in FATAL_WORDS):
        kind = "fatal"
    elif any(word in text for word in RATE_WORDS):
        kind = "rate_limit"
    elif any(word in text for word in RETRY_WORDS):
        kind = "retry"
    else:
        kind = "retry"
        message += "（未知业务返回，窗口内继续尝试）"
    delay = retry_after_seconds(response.headers.get("Retry-After"), settings.rate_limit_fallback) if kind == "rate_limit" else 0
    return Outcome(kind, message, code, status, delay, data)


def build_order_data(region_id, activity_url=ACTIVITY_URL):
    return {
        "activity_id": ACTIVITY_ID,
        "agent_channel": {
            "fromChannel": "",
            "fromSales": "",
            "isAgentClient": False,
            "fromUrl": activity_url
        },
        
        "business": {
            "id": 22755,
            "from": "lightningDeals"
        },
        "goods": [
            {
                "act_id": ACT_ID,
                "type": "bundle_budget_mc_lg4_01",
                "goods_param": {
                    "BlueprintId": "LINUX_UNIX",
                    "area": 1,
                    "ddocUnionConnect": 0,
                    "goodsNum": 1,
                    "imageId": "lhbp-eqora508",
                    "scenario": "0",
                    "timeSpanUnit": "12m",
                    "zone": "",
                    "regionId": region_id,
                    "type": "bundle_budget_mc_lg4_01"
                }
            }
        ],
        
        "preview": 0
    }


def buy_now(client, region_id, settings, activity_url=ACTIVITY_URL):
    response = None
    try:
        csrf_provider = getattr(client, "_csrf_provider", None)
        if callable(csrf_provider):
            token = csrf_provider()
            if token:
                try:
                    client.headers["x-csrf-token"] = token
                except (AttributeError, TypeError):
                    # 兼容测试替身等没有可写 headers 的客户端。
                    pass
        response = client.post(BUY_URL, json=build_order_data(region_id, activity_url),
                               timeout=settings.timeout, allow_redirects=False)
        return classify_response(response, settings)
    except requests.ConnectTimeout:
        return Outcome("retry", "建立连接超时，尚未提交订单")
    except requests.RequestException as exc:
        return Outcome("retry", f"{type(exc).__name__}：网络异常，窗口内继续尝试")
    finally:
        if response is not None:
            response.close()


class RunState:
    def __init__(self, region_ids):
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.winner = None
        self.reasons = []
        self.attempts = {rid: 0 for rid in region_ids}

    def publish(self, region_id, outcome):
        with self.lock:
            if outcome.kind == "success":
                if self.winner is None:
                    self.winner = (region_id, outcome.payload)
                self.stop.set()
            elif outcome.kind in ("fatal", "uncertain"):
                self.reasons.append((region_id, outcome.message))
                self.stop.set()

    def admit(self, region_id, deadline, max_attempts=None):
        """原子检查停止/窗口状态并登记一次在途请求，不因限流暂停补发。"""
        while not self.stop.is_set():
            with self.lock:
                now = time.monotonic()
                if self.stop.is_set() or now >= deadline:
                    return False
                if (max_attempts is not None and
                        self.attempts[region_id] >= max_attempts):
                    return False
                self.attempts[region_id] += 1
                return self.attempts[region_id]
        return False


def warm_connection(client, region_id, settings, state):
    """尽力预热连接；预热失败不能阻止到点正式下单。"""
    response = None
    try:
        response = client.post(CHECK_URL, json={
            "activity_id": ACTIVITY_ID,
            "goods": [{"act_id": ACT_ID, "region_id": [region_id]}],
            "preview": 0,
        }, timeout=(min(settings.timeout[0], 1), 1), allow_redirects=False)
        status = response.status_code
        if 200 <= status < 300:
            log(f"{REGION_NAMES.get(region_id, region_id)} 连接预热 HTTP={status}")
        else:
            log(f"{REGION_NAMES.get(region_id, region_id)} 连接预热 HTTP={status}；预热失败，开抢时继续尝试")
        if status == 429:
            state.publish(region_id, classify_response(response, settings))
    except requests.RequestException as exc:
        log(f"{REGION_NAMES.get(region_id, region_id)} 预热未完成：{type(exc).__name__}；开抢时继续尝试")
    finally:
        if response is not None:
            response.close()


def wait_until(target, stop):
    """最后 10 毫秒细化等待；不轮询远端时钟，不提前提交。"""
    while not stop.is_set():
        remaining = target - time.monotonic()
        if remaining <= 0:
            return True
        delay = min(remaining - 0.01, 0.5) if remaining > 0.02 else min(remaining, 0.001)
        stop.wait(delay)
    return False


def format_remaining(seconds):
    seconds = max(0, seconds)
    whole = int(seconds)
    millis = int((seconds - whole) * 1000)
    hours, remainder = divmod(whole, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"


def wait_with_countdown(target, stop, label):
    """等待目标时间并显示倒计时；临近目标时提高显示频率。"""
    next_log = 0
    while not stop.is_set():
        remaining = target - time.monotonic()
        if remaining <= 0:
            return True
        now = time.monotonic()
        if now >= next_log:
            log(f"{label}：{format_remaining(remaining)}")
            interval = 1 if remaining <= 60 else 10 if remaining <= 600 else 60
            next_log = now + interval
        if remaining <= 0.02:
            return wait_until(target, stop)
        stop.wait(min(remaining - 0.01, 1))
    return False


def buy_now_concurrent(region_ids, cookies, trigger=None, settings=None,
                       session_factory=create_session, warmup=True):
    settings = settings or Settings()
    region_ids = list(dict.fromkeys(region_ids))
    if not region_ids or len(region_ids) > 3:
        raise ValueError("请配置 1–3 个不同的地域")
    state = RunState(region_ids)
    activity_url = cookies.get("activity_url", ACTIVITY_URL) if isinstance(cookies, dict) else ACTIVITY_URL
    start = threading.Event()
    ready = {rid: threading.Event() for rid in region_ids}
    # 测试或即时调用未指定 trigger 时，在预热完成后设置统一起跑时间。
    timing = {}
    send_interval = settings.send_interval_ms / 1000
    lanes_per_region = settings.parallel_requests_per_region

    def worker(region_id):
        clients = []
        pending = {}

        def consume(future):
            attempt, sent = pending.pop(future)
            try:
                outcome = future.result()
            except Exception as exc:
                outcome = Outcome("fatal", f"请求线程异常：{type(exc).__name__}")
            # 先发布停止信号，再输出日志，不让控制台 I/O 延迟停止。
            state.publish(region_id, outcome)
            elapsed = (time.monotonic() - sent) * 1000
            sent_offset = (sent - timing["target"]) * 1000
            message = outcome.message.replace("\n", " ").replace("\r", " ")[:240]
            log(f"{REGION_NAMES.get(region_id, region_id)} 第{attempt}/{settings.max_attempts}次 "
                f"发送=开抢后{sent_offset:+.1f}ms 耗时={elapsed:.1f}ms "
                f"HTTP={outcome.http_status} code={outcome.code} "
                f"{outcome.kind}: {message}")

        try:
            # 每条通道使用独立 Session，避免并发操作同一个 requests.Session。
            for _ in range(lanes_per_region):
                client = session_factory(cookies)
                clients.append(client)
                if warmup and not state.stop.is_set():
                    warm_connection(client, region_id, settings, state)
            ready[region_id].set()
            while not start.wait(0.02):
                if state.stop.is_set():
                    return
            deadline = timing["deadline"]
            next_send = timing["target"]
            next_lane = 0
            exhausted = False
            while not state.stop.is_set():
                # 先回收已经完成的请求；未完成的请求不阻塞新的通道。
                for future in list(pending):
                    if future.done():
                        consume(future)
                if state.stop.is_set():
                    break
                now = time.monotonic()
                while (now < deadline and now >= next_send and
                       len(pending) < lanes_per_region and not state.stop.is_set()):
                    attempt = state.admit(region_id, deadline, settings.max_attempts)
                    if not attempt:
                        exhausted = state.attempts[region_id] >= settings.max_attempts
                        break
                    client = clients[next_lane]
                    next_lane = (next_lane + 1) % len(clients)
                    sent = time.monotonic()
                    future = request_executor.submit(buy_now, client, region_id,
                                                     settings, activity_url)
                    pending[future] = (attempt, sent)
                    next_send += send_interval
                    now = time.monotonic()
                if state.stop.is_set() or now >= deadline:
                    break
                if exhausted and not pending:
                    break
                if pending:
                    wait_timeout = min(max(0, next_send - time.monotonic()), 0.02)
                    wait(list(pending), timeout=wait_timeout,
                         return_when=FIRST_COMPLETED)
                else:
                    state.stop.wait(min(max(0, next_send - time.monotonic()), 0.02))

            # 窗口结束或某地域成功后，等待在途请求收尾，再关闭各自 Session。
            for future in list(pending):
                consume(future)
        except Exception as exc:
            state.publish(region_id, Outcome("fatal", f"线程异常：{type(exc).__name__}"))
        finally:
            ready[region_id].set()
            for client in clients:
                client.close()

    with ThreadPoolExecutor(max_workers=len(region_ids) * lanes_per_region) as request_executor:
        with ThreadPoolExecutor(max_workers=len(region_ids)) as scheduler_executor:
            futures = [scheduler_executor.submit(worker, rid) for rid in region_ids]
            try:
                for event in ready.values():
                    while not event.is_set() and not state.stop.is_set():
                        remaining = 0.02 if trigger is None else trigger - time.monotonic()
                        if remaining <= 0:
                            break
                        event.wait(min(remaining, 0.02))
                target = time.monotonic() if trigger is None else trigger
                timing["target"] = target
                timing["deadline"] = target + settings.window_seconds
                if wait_with_countdown(target, state.stop, "距离开抢还有"):
                    start.set()
                    log(f"开抢，{len(region_ids)}个地域各 {lanes_per_region} 条并行通道，未等待响应直接补发")
                for future in as_completed(futures):
                    future.result()
            except BaseException:
                state.stop.set()
                # 让尚未起跑的线程退出；先放置 deadline，防止读未初始化数据。
                timing.setdefault("deadline", time.monotonic())
                start.set()
                raise
    if state.winner:
        log(f"接口报告成功：地域 {state.winner[0]}；后续提交已停止")
        log("成功业务响应：" + json.dumps(state.winner[1], ensure_ascii=False))
    for region_id, reason in state.reasons:
        log(f"停止原因（地域 {region_id}）：{reason}")
    log(f"各地域下单尝试次数：{state.attempts}")
    return state


def calibrate_clock(settings, activity_url=ACTIVITY_URL):
    """HTTP Date 只有秒级精度，用最低往返耗时样本估计偏移。"""
    samples = []
    with requests.Session() as client:
        for _ in range(3):
            wall = time.time()
            before = time.monotonic()
            response = None
            try:
                response = client.head(activity_url, timeout=settings.timeout,
                                       headers={"Cache-Control": "no-cache"}, allow_redirects=False)
                elapsed = time.monotonic() - before
                if not 200 <= response.status_code < 400 or float(response.headers.get("Age", 0)) > 0:
                    continue
                server = parsedate_to_datetime(response.headers["Date"])
                if server.tzinfo is None:
                    server = server.replace(tzinfo=timezone.utc)
                # Date 是整秒，用该秒区间中点估计，误差至少约 ±500ms。
                offset = server.timestamp() + 0.5 - (wall + elapsed / 2)
                samples.append((elapsed, offset))
            except (requests.RequestException, KeyError, ValueError, TypeError, OverflowError):
                continue
            finally:
                if response is not None:
                    response.close()
    if not samples:
        log("服务器校时失败，使用系统时钟")
        return 0
    elapsed, offset = min(samples)
    log(f"估计时钟偏移 {offset * 1000:+.0f}ms，RTT {elapsed * 1000:.0f}ms；HTTP Date 不提供毫秒级授时")
    return offset


def next_rush_time(now):
    """返回北京时间的下一场：每天 10:00 和 15:00，不使用硬编码日期。"""
    if now.tzinfo is None:
        raise ValueError("now 必须包含时区")
    now = now.astimezone(BEIJING)
    for hour in DAILY_RUSH_HOURS:
        candidate = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if candidate > now:
            return candidate
    tomorrow = now + timedelta(days=1)
    return tomorrow.replace(hour=DAILY_RUSH_HOURS[0], minute=0, second=0, microsecond=0)


def main():
    settings = Settings()
    browser_session = start_browser_credentials(load_activity_url())
    try:
        credentials = browser_session["credentials"]
        activity_url = credentials["activity_url"]
        offset = calibrate_clock(settings, activity_url)
        corrected_now = datetime.fromtimestamp(time.time() + offset, BEIJING)
        target_datetime = next_rush_time(corrected_now)
        target = target_datetime.timestamp()
        trigger = time.monotonic() + target - (time.time() + offset)
        target_text = target_datetime.strftime("%Y-%m-%d %H:%M:%S")
        log(f"当前校准北京时间 {corrected_now.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]}；"
            f"自动选择下一场 {target_text}")
        log(f"每地域最多 {settings.max_attempts} 次；"
            f"并行通道 {settings.parallel_requests_per_region} 条；"
            f"通道补发间隔 {settings.send_interval_ms}ms；"
            f"连接/读取超时 {settings.connect_timeout_ms}/{settings.read_timeout_ms}ms")
        wait_with_countdown(trigger - settings.warmup_seconds, threading.Event(), "距离连接预热还有")
        state = buy_now_concurrent(REGION_IDS, credentials, trigger=trigger, settings=settings)
        if not state.winner:
            try:
                refresh_browser_credentials(browser_session)
            except Exception as exc:
                log(f"失败后重新获取浏览器凭据失败：{type(exc).__name__}：{exc}")
        return 0 if state.winner else 1
    finally:
        browser_session["browser"].close()
        browser_session["playwright"].stop()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        log("已中止；不再发起后续请求")
        raise SystemExit(130)
    except (OSError, ValueError) as exc:
        log(f"配置或 Cookie 错误：{exc}")
        raise SystemExit(1)
