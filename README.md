# TencentCloudFlashSaleScript / 腾讯云服务器抢购脚本

## 本次采用的方案

浏览器登录后获取 Cookie 和有效 CSRF，分别保存到 `cookies.json` 与 `config.json`，Python requests 使用三个独立 Session 定时发送购买请求。不需要先抢到商品，也不要求抓到真实购买包。

比较参考（2026 年检索到的项目，不代表作者成功率已核验）：

| 方式 | 参考 | 本项目取舍 |
| --- | --- | --- |
| 浏览器登录后获取 Cookie、requests 下单 | [Ezio，8 月 28 日](https://github.com/Ezio1Alex/tencent-server-snap) | 采用；适合现有独立线程结构 |
| 浏览器内 fetch 下单 | [djs，8 月 15 日](https://github.com/djs-91/tencent-server-seckill)、[avelli，6 月](https://github.com/avelli/tencentyun-qianggou) | 可行候选，但无证据证明优于直接 HTTP；本次不引入第二执行路径 |
| 监听普通活动 API 获取 CSRF | [ghajg，8 月 12 日](https://github.com/ghajg/tencentyun-snake-up) | 参考过；当前改为手动配置实测值 |
| 时间窗口内循环尝试 | [kkkksad，8 月 4 日](https://github.com/kkkksad/tenxunyun) | 采用窗口和次数双重限制，不照搬高并发 |

保留已确认的 activity_id、goods[].act_id 以及现有商品请求体，不自动替换 ID，不盲目切换不同仓库的 type/business 字段。现有接口和结构有近期项目佐证，但未通过本账号真实下单验证，不能承诺成功率。

## PowerShell 7 环境

```powershell
cd D:\project\tencentyun-snake-up
# 已有 .venv 时不必重新创建
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install requests playwright
python -m playwright install chromium
```

本机已有 Python 3.13 虚拟环境。也可以不激活，直接使用下面的命令。

## 1. 启动并登录

现在不需要单独运行旧的凭据采集脚本。每次直接启动主脚本，它会打开可见浏览器，等待扫码登录，登录后从活动页的 `check_goods/check-available` 请求捕获非 `0` CSRF，并读取当前 Cookie；浏览器会一直保持打开，直到本轮抢购结束。

```powershell
.\.venv\Scripts\python.exe snap_up_server.py
```

1. 浏览器打开带活动页回跳参数的登录页，正常扫码登录；脚本会等待自动跳转。若地址变化，也可以在同一浏览器中手动打开当前活动页。
2. 登录后脚本丢弃登录前的 `0` 或旧 token，刷新活动页，只接受登录后接口中的有效 CSRF。
3. 终端会打印本轮 CSRF 和 Cookie 数量，便于观察；Cookie 原文只保存到本地，不打印到聊天或日志文件。
4. 若本轮没有成功结果，脚本会在同一浏览器中再次刷新活动页，重新获取并打印 CSRF/Cookie 数量，然后关闭浏览器。

采集期间拦截 `do-goods`，防止浏览器页面误点购买；实际下单仍由 Python requests 的三个独立 Session 完成。

如果页面提示“浏览器还未开启本地网络访问权限”，脚本会尝试自动授予 Playwright 当前浏览器上下文的 `local-network-access` 权限。若仍提示，在地址栏左侧的网站设置中将“本地网络访问”设为“允许”，Chrome 145 及以后可能显示为“本地网络”或“设备上的应用”，然后刷新页面。这个权限只影响微信快捷登录访问本机/局域网服务，不影响 Cookie 文件和 CSRF 配置；也可以直接使用二维码登录。

`cookies.json` 和 `config.json` 都含登录或请求凭据，不要分享或提交，已加入 Git 忽略。

## 活动地址配置（可选）

默认使用当前活动地址，通常不需要创建 `config.json`。如果活动页地址变化，可以复制示例文件并只修改 `activity_url`；`csrf_token` 会在本次扫码登录后由主脚本自动覆盖，不需要手工维护：

```powershell
Copy-Item .\config.example.json .\config.json
notepad .\config.json
```

配置格式：

```json
{
  "activity_url": "https://cloud.tencent.com/act/pro/featured-202607?fromSource=gwzcw.11914902.11914902.11914902&utm_medium=cpc&utm_id=gwzcw.11914902.11914902.11914902&gad_source=1&gad_campaignid=1480177331&gbraid=0AAAAADDDx-rm1Uu3nfP4-7MwQ9R7lPkOL&gclid=Cj0KCQjw--7UBhCpARIsAGJBptiLarwO5_Xu1C_pjWIsMEwGyWgvUb-LFyYzA37Xif4EfLcbHUhPXqEaAoQGEALw_wcB"
}
```

`snap_up_server.py` 会在登录成功后将本轮有效 CSRF 写入 `config.json`，并将 Cookie 写入 `cookies.json` 作为留存；实际抢购优先使用当前浏览器内存中的最新值。如果你手动确认了新的 CSRF，也可以直接修改 `config.json`，但下次启动仍会以登录后的实时值为准。

## 2. 启动

不再配置硬编码日期。程序启动后先估计服务器时钟偏移，再按北京时间自动选择下一场：每天 10:00 和 15:00。10:00 整启动会选择当天 15:00，15:00 整或之后启动会选择次日 10:00。需要调整地域时修改 `REGION_IDS = [1, 4, 8]`。

```powershell
.\.venv\Scripts\python.exe snap_up_server.py
```

主程序启动时直接创建浏览器登录会话，不再依赖上一次保存的 Cookie/CSRF 才能开始；两个 JSON 文件只作为本地留存和活动地址配置。浏览器在等待、预热和抢购过程中不关闭，最终流程结束后才关闭。

## 发送和重试参数

| Settings 参数 | 默认值 | 含义 |
| --- | ---: | --- |
| max_attempts | 60 | 每地域最多 60 次，包含首次 |
| window_seconds | 30 | 到点后 30 秒停止新提交 |
| send_interval_ms | 20 | 在途通道未满时，下一条请求的最小发出间隔，不等待上一条响应 |
| parallel_requests_per_region | 3 | 每地域独立 requests Session 通道数 |
| retry_base_ms / retry_max_ms | 100 / 300 | 兼容旧配置，新的并行补发主循环不再使用 |
| connect_timeout_ms | 3000 | 连接超时 |
| read_timeout_ms | 5000 | 单次等待响应 5 秒，给 30 秒窗口保留再次尝试机会 |
| warmup_seconds | 5 | 提前 5 秒用 check-available 预热 |
| rate_limit_fallback | 1 | 兼容旧返回结构，新的补发调度不因限流等待 |

- 每地域默认 3 个独立 Session 通道，总并发最多 9 个；某条请求未返回时，其余通道仍按 `send_interval_ms` 补发。
- Python Session 使用 `cookies.json` 中的 Cookie、`config.json` 中的 CSRF 和默认浏览器 User-Agent，以及活动页 Referer、Origin 和 XMLHttpRequest 头。正常接收 Set-Cookie，TLS 校验保持开启，HTTP 库不暗中重试 POST。
- 预热不使用购买接口，连接/读取各最多等待 1 秒；失败不终止正式抢购。
- 即使 HTTP 400 也解析业务 JSON，保留 code/msg。未开始、库存不足、繁忙、下单人数过多等明确消息允许窗口内重试。
- HTTP 429 或明确业务限流只记录为 `rate_limit`，不会暂停其他地域或补发通道；未核验数字业务码不硬编码。业务 408 不直接等同于 HTTP 408。
- HTTP 2xx 且 code 为整数 0 或字符串 "0" 时报告成功，打印成功业务响应，停止新请求。不增加订单查询和自动支付。
- 登录、资格、参数等明确不可重试错误停止并显示原因。未知非零业务返回、HTTP 5xx 非 JSON、响应缺少 code、读取超时和连接中断会在窗口与次数限制内继续尝试。

窗口到期不强制杀死已发送请求，因此程序可能在开抢窗口结束后继续等待响应。requests 的读取超时不是整个请求绝对总耗时限制。并发可能生成多笔订单，停止不能撤回已发请求。

HTTP Date 校时精度只有秒级，毫秒日志不代表毫秒级授时。长时间等待不会刷新服务端登录态，建议临场运行。

## 离线测试

```powershell
.\.venv\Scripts\python.exe -B -m unittest -v test_snap_up_server test_auth
```

全部使用模拟 HTTP/浏览器，测试 Cookie/CSRF 分离、购买拦截、配置校验、确认 ID 不变、400 业务分类、繁忙重试、并发与停止等行为，不访问腾讯云、不购买。

脚本运行正常或离线测试通过不等于一定抢到；库存、账号资格、服务端规则仍决定结果。请遵守活动规则和请求限流。
