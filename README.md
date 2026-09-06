# TencentCloudFlashSaleScript

腾讯云轻量应用服务器活动抢购脚本。程序使用 Playwright 完成登录态获取，使用 Python `requests` 发起抢购请求。

## 功能

- 自动打开可见浏览器，扫码登录腾讯云。
- 登录后从活动页接口获取有效 CSRF 和 Cookie。
- 浏览器在等待和抢购期间保持打开，避免登录态失效或凭据不同步。
- 广州、上海、北京三个地域并行抢购。
- 每个地域使用独立 Session 和多个请求通道，响应未返回时仍可继续补发请求。
- 自动校准服务器时间，并选择北京时间每天 10:00 或 15:00 的下一场活动。
- 预热连接、处理 HTTP 400/429、库存不足、系统繁忙和限流等可重试结果。

## 环境

- Windows
- Python 3.13+
- PowerShell 7
- Chromium

```powershell
cd D:\project\tencentyun-snake-up
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install requests playwright
python -m playwright install chromium
```

已有 `.venv` 时无需重复创建环境。

## 运行

直接运行主脚本：

```powershell
.\.venv\Scripts\python.exe snap_up_server.py
```

运行流程：

1. 打开腾讯云登录页，扫码完成登录。
2. 自动回到活动页并刷新页面。
3. 捕获登录后的有效 CSRF，读取当前浏览器 Cookie。
4. 输出 CSRF 和 Cookie 数量，等待下一场活动。
5. 开抢后按地域并行发送请求，并在窗口内持续补发。
6. 如果没有成功结果，刷新同一浏览器页面并重新获取凭据。

浏览器采集阶段会拦截 `do-goods`，防止页面误触下单；实际抢购请求由脚本发送。

## 活动配置

默认活动地址写在 `snap_up_server.py` 中。需要更换活动地址时，复制示例配置：

```powershell
Copy-Item .\config.example.json .\config.json
notepad .\config.json
```

配置示例：

```json
{
  "activity_url": "https://cloud.tencent.com/act/pro/活动路径"
}
```

CSRF 会在本次登录后自动更新，不需要手动填写。当前活动的 `activity_id`、`goods[].act_id`、商品类型和地域配置位于 `snap_up_server.py`。

## 抢购参数

参数位于 `Settings`：

| 参数 | 默认值 | 说明 |
| --- | ---: | --- |
| `max_attempts` | 60 | 每个地域的最大请求次数 |
| `window_seconds` | 30 | 开抢后允许发起新请求的时间窗口 |
| `send_interval_ms` | 20 | 请求通道未满时的最小补发间隔 |
| `parallel_requests_per_region` | 3 | 每个地域的独立请求通道数 |
| `warmup_seconds` | 5 | 提前预热连接的秒数 |
| `connect_timeout_ms` | 3000 | 连接超时 |
| `read_timeout_ms` | 5000 | 单次读取响应超时 |

默认配置下最多同时运行 9 个抢购请求通道。限流和繁忙响应会记录日志，但不会暂停其他通道；成功或明确不可重试的错误会停止后续新请求。

地域配置：

```python
REGION_IDS = [1, 4, 8]
```

## 文件说明

| 文件 | 用途 |
| --- | --- |
| `snap_up_server.py` | 主程序、浏览器登录、凭据采集和抢购逻辑 |
| `config.example.json` | 活动地址配置示例 |
| `test_snap_up_server.py` | 抢购和重试逻辑的离线测试 |
| `test_auth.py` | 登录态和凭据采集的离线测试 |
| `config.json` | 本地活动配置和 CSRF，运行时生成，已忽略 |
| `cookies.json` | 本地登录 Cookie，运行时生成，已忽略 |

## 常见问题

### 浏览器提示未开启本地网络访问权限

脚本会尝试为腾讯云活动页授予 `local-network-access` 权限。如果仍然提示，请在浏览器地址栏左侧的网站权限中允许“本地网络”，然后刷新页面；也可以直接使用二维码登录。

### 没有捕获到有效 CSRF

确认已经扫码登录，并且浏览器已经回到 `https://cloud.tencent.com/act/...` 活动页。脚本会忽略登录前通常为 `0` 的 CSRF，只接受登录后活动接口中的有效值。

### 运行后没有抢到

终端日志中的 `发送=开抢后...ms` 表示请求实际发出时间，`耗时` 表示请求完成时间。抢购结果还取决于库存、账号资格、服务端限流和其他用户的请求速度。

## 测试

测试使用模拟浏览器和 HTTP 客户端，不访问腾讯云，也不会下单：

```powershell
.\.venv\Scripts\python.exe -B -m unittest -v test_snap_up_server test_auth
```

## 安全提示

`config.json` 和 `cookies.json` 包含登录或请求凭据，不要上传、分享或提交到 Git。请遵守腾讯云活动规则和接口访问限制。
