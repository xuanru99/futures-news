# 期货外盘新闻简报推送机器人

每天早晚自动抓取国外主流财经媒体（WSJ / CNBC / MarketWatch / FXStreet / OilPrice / Mining.com / World Grain）的最新新闻，按板块分类去重后生成简报，推送到你的微信。

## 目录结构

```
futures-news/
├── news_bot.py           # 主脚本（纯 Python 标准库，无需安装任何依赖）
├── config.json           # 配置：推送 key、代理、新闻源开关
├── register_tasks.bat    # 注册每天 08:00 / 17:00 的定时任务（双击运行）
├── unregister_tasks.bat  # 取消定时任务
└── digests/              # 每次生成的简报存档（.md 文件）
```

## 一次性配置（5 分钟）

### 第 1 步：注册微信推送通道（二选一，推荐 Server酱）

**方式 A：Server酱（推荐，免费额度每天 5 条，够用）**
1. 浏览器打开 https://sct.ftqq.com
2. 微信扫码登录
3. 在「Key」页面复制你的 SendKey（形如 `SCT123456xxxxxxxx`）
4. 打开 `config.json`，填入：
   ```json
   "push": {
     "serverchan_key": "SCT123456xxxxxxxx",
     "pushplus_token": ""
   }
   ```
5. 微信扫码关注「方糖」服务号（页面会有指引），消息就推到这个服务号里

**方式 B：PushPlus**
1. 浏览器打开 https://www.pushplus.plus 微信扫码登录
2. 复制 token，填入 `config.json` 的 `pushplus_token`

### 第 2 步：测试推送

```
"C:\Users\10702\.workbuddy\binaries\python\versions\3.13.12\python.exe" news_bot.py
```

微信上收到消息即成功。

### 第 3 步：注册每天早晚定时任务

双击 `register_tasks.bat`，会注册两个任务：
- **FuturesNewsMorning** — 每天 08:00，晨报（回溯最近 16 小时，覆盖隔夜美盘）
- **FuturesNewsEvening** — 每天 17:00，晚报（回溯最近 9 小时，覆盖白天时段）

> ⚠️ 电脑必须在那个时间点处于开机状态，任务才会执行。想改时间：右键编辑 bat 里的 `/st 08:00`。

## 常用命令

```
python news_bot.py --test          # 只生成简报存到 digests/，不推送
python news_bot.py --hours 12      # 自定义回溯最近 12 小时
python news_bot.py morning         # 晨报模式（16 小时窗口）
python news_bot.py evening         # 晚报模式（9 小时窗口）
```

## 新闻源说明

**直连可用（默认开启）**：WSJ、CNBC、MarketWatch、FXStreet、OilPrice、Mining.com、World Grain —— 覆盖宏观、能源、金属、农产品。

**需要代理才能访问（默认关闭）**：Google News、彭博。
如果你电脑上有梯子（如 Clash 默认端口 7890），在 `config.json` 里：
```json
"proxy": "http://127.0.0.1:7890"
```
再把对应源的 `"enabled": false` 改成 `true` 即可。

## 调整板块与数量

- `max_per_section`：每个板块最多条数（默认 8）
- `max_total`：简报总条数上限（默认 45）
- 关键词分类规则在 `news_bot.py` 的 `KEYWORDS` 里，可自行增删
- 不想要某类消息：把 `config.json` 里对应源的 `"enabled"` 改为 `false`

## 已知限制

- 抓取的是**标题 + 链接 + 时间**（外媒正文基本都有付费墙），点链接去原文看详情
- 标题保留英文原文——更快更准确，重要数据（非农/CPI/EIA）一眼能认出来
- 推送依赖你的电脑开机运行；想要 7×24 不间断，可以把脚本部署到云函数/服务器上（需要时再说）
