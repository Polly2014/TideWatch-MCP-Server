# CLAUDE.md — 观潮 (TideWatch)

## Project Overview

AI 投研搭档 MCP Server — 多维融合股票分析引擎。不是仪表盘，而是一个**可编程的投研引擎**。

核心理念：**不是给你看数据，而是跟你聊投资。**

## Commands

```bash
cd X-Workspace/TideWatch-MCP-Server
poetry install              # 安装依赖
poetry run tidewatch        # 本地模式 (stdio)
poetry run tidewatch --http --port 8889  # 远程模式 (HTTP)

# 冒烟测试（每次改完必跑）
poetry run python tests/smoke_test.py --quick   # 快速 ~40s, 9个核心工具
poetry run python tests/smoke_test.py           # 完整 ~3min, 14个全量工具
```

## Architecture

```
TideWatch-MCP-Server/
├── pyproject.toml          # Poetry 配置 + 入口点
├── config.env              # 环境变量
├── setup.sh                # 一键安装脚本 (Azure VM)
├── src/
│   └── tidewatch/          # Python 包 (import tidewatch.xxx)
│       ├── __init__.py
│       ├── server.py       # ⭐ MCP 主入口 (FastMCP + stdio/HTTP 双模式, 14 工具)
│       ├── data.py         # 数据层 (baostock 日K线 + AKShare 资金/新闻)
│       ├── technical.py    # 技术分析引擎
│       ├── regime.py       # 市场体制识别
│       ├── narrative.py    # 叙事式分析报告生成
│       ├── llm.py          # LLM 叙事润色 (CopilotX + Claude Sonnet 4)
│       ├── tracker.py      # 信号追踪系统 (SQLite, 当天+同score去重)
│       ├── guardrails.py   # 行为护栏 (Anti-FOMO, 3条规则)
│       └── portfolio.py    # 三级股票池 (持仓+自选+热门24只)
├── config/                 # 部署配置
│   ├── nginx_tidewatch.polly.wang.conf  # Nginx 反向代理
│   └── mcp_remote.example.json         # 客户端配置示例
├── scripts/                # 部署脚本
│   ├── setup_domain.sh     # DNS + Nginx + SSL 一键配置
│   └── tidewatch.service   # systemd 服务文件
└── data/                   # 运行时数据 (git-ignored, 仅 Azure VM 上有实际数据)
    └── signals.db          # 信号追踪数据库 (持仓/自选/账户/信号全在远程 VM)
```

注意：Phase 1-4 全部完成，Phase 5 前两项（信号回填 + 复盘看板）已完成。Dashboard 本地维护（`static/tidewatch.html`），不走 git push。HOT_POOL 已从 76 只精简至 24 只（8板块×3龙头），扫描耗时从 ~22s 降至 ~4.25s。

**⚠️ 数据库在远程 Azure VM 上**：`data/signals.db`（含持仓、自选、账户资金、信号记录）只在 Azure VM 有实际数据，本地仅有空库。排查数据问题时必须 SSH 到远程查询：
```bash
ssh -F ssh.config Azure-Server "sqlite3 ~/GitHub_Workspace/TideWatch-MCP-Server/data/signals.db 'SELECT * FROM holdings;'"
```

## MCP Tools

| Tool | 用途 |
|------|------|
| `analyze_stock` | ⭐ 核心：个股综合分析（技术+资金+消息+体制），支持 `skip_llm=true` 跳过润色秒出 |
| `get_regime` | 今日潮势速读（牛/熊/横盘/高波动） |
| `compare_stocks` | 多股横向对比 |
| `get_money_flow_detail` | 资金流向详细分析 |
| `get_stock_news_report` | 个股新闻消息面 |
| `get_north_flow_report` | 北向资金分析 |
| `polish_narrative_llm` | LLM 叙事润色（配合 skip_llm 渐进加载用）|
| `review_signals` | 查看历史信号和胜率统计 |
| `update_signal_outcomes` | 回填历史信号实际走势 |
| `scan_market` | 三级股票池扫描（持仓+自选+热门24只，串行K线+技术评分）5min缓存，asyncio.to_thread 不阻塞事件循环 |
| `manage_holdings` | 持仓管理（添加/移除/查看，带买入价和数量）|
| `manage_watchlist` | 自选股管理（添加/移除/查看，可备注关注原因）|
| `manage_account` | 账户资金管理（可用资金/总资产/持仓市值）|
| `server_status` | 服务器状态 |

## Design Principles

1. **多维交叉验证** — 技术面+资金面+消息面+市场体制，四维交叉
2. **冲突检测** — "技术面看多但主力在出货"这种矛盾才是真金
3. **体制感知** — 横盘市里看什么都是观望，牛市里回调就是机会
4. **MCP-Native** — 在 Claude/Cursor 中直接使用，UI 只是引擎的皮肤
5. **数据驱动进化** — 每次策略调整必须有回填数据支撑，详见 [策略进化日志](docs/strategy-evolution.md)

### 信号阈值 (v2, 2026-03-30)

基于 52 条回填数据调整，详细分析见 [docs/strategy-evolution.md](docs/strategy-evolution.md)：

| 评分 | 信号 | 备注 |
|------|------|------|
| ≥ +50 | 看多 | v2 提高门槛（v1 是 +25），回填 [+50,+75) 100% 正确 |
| +25 ~ +50 | 偏多 / 中性 | bear/mild_bear 体制下强制中性（回填 0/4 全错） |
| +8 ~ +25 | 偏多 | |
| -8 ~ +8 | 中性观望 | |
| -25 ~ -8 | 偏空 | |
| ≤ -25 | 看空 | 81.6% 胜率，不调整 |

## Roadmap

### Phase 1: ✅ MCP Engine (2026-03-11)
- [x] AKShare 数据接入（日K线 + 资金流向 + 新闻 + 北向资金 + 龙虎榜）
- [x] 技术分析（8维评分：MA/RSI/MACD/KDJ/BOLL/ATR/OBV/形态识别）
- [x] 市场体制识别（6种：牛/熊/横盘/高波动/震荡偏强/震荡偏弱）
- [x] 冲突检测（5种矛盾信号：技术vs资金、个股vs大盘、量价背离等）
- [x] 叙事式分析报告（形态驱动开场 + 多空博弈 + 有立场结论）
- [x] 代理兼容（NO_PROXY + 失败冷却60s + 日K线fallback）

### Phase 2: ✅ 引擎增强 (2026-03-12)
- [x] 信号追踪系统（SQLite，每次分析自动记录，5/10/20日胜率回填）
- [x] 行为护栏 v1（追高检测 / 分析频次提醒 / 连续看空检测）
- [x] scan_market 工具（全市场扫描 Top/Bottom N 强弱股）

### Phase 3: ✅ 深度进化 (2026-03-13)
- [x] LLM 叙事润色（CopilotX API + Claude Sonnet 4，失败 fallback 模板叙事）(2026-03-12)
- [x] scan_market v2 — 三级股票池扫描（持仓+自选+热门24只，串行K线+技术评分，绕过 push2 反爬）5min缓存 (2026-03-13)
- [x] manage_holdings / manage_watchlist — 持仓管理（带买入价）+ 自选股管理（SQLite）(2026-03-13)
- [x] manage_account — 账户资金管理（可用资金/总资产/持仓市值）(2026-03-18)
- [x] HOT_POOL 瘦身 76→24 只（8板块×3龙头），扫描 ~22s→~4.25s (2026-03-18)

### Phase 4: ✅ 触达层 (2026-03-13)
- [x] Azure VM 远程部署代码准备（FastMCP HTTP 双模式 + API Key 认证 + Nginx + systemd）(2026-03-12)
- [x] Azure VM 实际部署（`tidewatch.polly.wang/mcp`，Cloudflare DNS + Let's Encrypt SSL）(2026-03-12)
- [x] Web Dashboard 主体 — `static/tidewatch.html` 本地维护 (2026-03-13):
  - 前端直接 fetch MCP JSON-RPC（`mcpCall('scan_market')` 等）
  - 持仓（浮盈/浮亏 + 买入价/数量 meta）+ 自选 + 热门三级展示
  - 顶部：市场体制 regime badge | 持仓总浮盈 | 看多/看空比
  - Skeleton + localStorage Cache (24h) + Split-Phase Init + Fade 过渡
  - Sparkline 7日趋势（无数据灰色 `---` 占位）
  - 冲突检测高亮（Apple 柔光 box-shadow + 7px 琥珀脉冲点 `::after`）
  - Hover 交互（Score legend / section tints / card 悬浮阴影）
  - 小龙虾 Review: 9.5/10 + 9/10 两轮
- [x] Web Dashboard — 个股详情面板 (2026-03-13):
  - 点击卡片 → `analyze_stock(skip_llm=true)` 秒出四维分析
  - 异步 `polish_narrative_llm` 火并忘 — LLM 完成后直接显示最终版
  - 双栏布局：左(四维卡片+冲突检测+建议仓位) / 右(🤖 AI 深度分析)
  - 休盘缓存：`isMarketOpen()` 判断，休盘期间 0ms 秒开
  - overlay fade-in (opacity+visibility 过渡)
  - 左右键切换 + Esc 关闭

### Phase 5: 信号验证 + 主动触达 (2026-03-19)
- [x] 信号回填 — `update_signal_outcomes` 首批 5d 数据，13条回填，5d胜率76.9% (2026-03-19)
  - 修复 `update_outcomes()` SQL SELECT 缺少 price_5d/10d/20d 列的 bug
  - `update_signal_outcomes` 包装 `asyncio.to_thread()` 不阻塞事件循环
- [x] 信号复盘看板 — Dashboard 新 Tab「🎯信号复盘」(2026-03-19)
  - Tab 切换（📊实时面板 | 🎯信号复盘），自动刷新仅在实时面板触发
  - 5 张统计卡（5d胜率 / 看多胜率 / 看空胜率 / 平均收益 / 已回填）
  - 4 个过滤器（全部 / ✅正确 / ❌错误 / ⏳待验证）
  - 日期分组时间线，卡片风格对齐主面板（box-shadow 柔光 + hover 上浮）
  - 正确卡片浅绿底 + 绿色光晕 / 错误卡片浅红底 + 红色光晕 / 待验证降透明度
  - 前端去重（同天同股保留最后分析）+ 后端当天去重（同symbol+同score）
  - 样本不足（N≤2）显示灰色提示而非误导性百分比
  - 历史重复数据清理（60→29条）+ 回填按钮一键触发
- [ ] 盘前播报 v1 — cron + Telegram Bot，每日 9:15 推送持仓+体制+异动 (目标: 下周)
  - 内嵌体制切换告警（bull→volatile 等状态跃迁，比涨跌更值得推送）
- [x] 美股支持 — yfinance 数据源 + 市场路由 + SPY 体制识别 (2026-03-20) 🦞 9/10
  - `is_us_stock()` 自动识别（字母=美股，数字=A股）
  - yfinance K线接入（免费、零反爬、全球市场），`start` 参数替代 `period`（精确控制范围）
  - 美股资金流/新闻/北向等 A 股特色接口返回空（graceful fallback）
  - regime 用 SPY 替代沪深300，scan_market 按市场分别拉 A股/美股 regime
  - `get_us_stock_name` 内存缓存（避免扫描循环重复 HTTP 请求）
  - guardrails 追高阈值 A股 8% / 美股 15%（美股无涨跌停，波动更大）
  - narrative 美股补充 SPY 相对强弱替代资金面段落
  - LLM 润色区分"A股分析师" vs "美股分析师"角色和交易规则
  - 持仓上下文 ¥→$，去掉1手限制
  - TODO: SPY 自分析时 regime 会自引用（无 market context）
  - Dashboard 美股适配 🦞 9.5/10：`isUSCode()`/`cur()`/`displayName()` 三件套，8处币种 ¥→$ + ticker主标题 + 混合币种摘要栏
- [ ] 回测引擎 v1 — baostock 历史数据 + TideWatch 信号策略回测 vs 沪深300 (目标: 九坤面试前)
- [x] 数据增强 T1 — 已有存货接入主分析流 (2026-03-21) 🦞 9/10
  - T1-1: 龙虎榜并发拉取+注入 report（AKShare 新版 API 适配，保留在 report 不喂 LLM）
  - T1-2: 新闻标题喂给 LLM prompt（A股 AKShare + 美股 yfinance，polish_narrative_llm 同步更新）
  - T1-3: 美股新闻 `yf.Ticker.news`（解析 `content.title` 嵌套格式，不再跳过美股新闻）
- [x] 数据增强 T2 — 补核心短板 (2026-03-21)
  - T2-1: PE/PB 从 baostock K线取（peTTM+pbMRQ，零额外请求，之前永远是 0）
  - T2-2: 换手率 turn 字段 + 异常检测（>8% 高换手 / 突降萧条），与量比+OBV 三角交叉
  - T2-3: LLM 结构化输入 — data_summary 含评分/均线/动量/量能/换手率/估值/布林/体制/冲突，prompt 从"润色"升级为"半分析"（300字）
  - T2-4: 北向资金 → 移入冰箱（当前不阻塞）

### 冰箱（Icebox）— 条件成熟再做
- 产业链图谱（HOT_POOL 按板块组织已部分覆盖）
- 雪球数据源（baostock 够用，反爬维护成本高）
- 多因子 ML 模型（需 500+ 信号样本量）
- 情绪面接入（爬虫维护成本高）
- 移动端适配（当前桌面端优先）
- Dashboard dark mode（小龙虾建议的最高 ROI UI 改进）

## Deployment

### Azure VM

SSH 配置见 `ssh.config`（git-ignored），快捷连接：
```bash
ssh -F ssh.config Azure-Server
```

服务管理：
```bash
ssh -F ssh.config Azure-Server "sudo systemctl status tidewatch"   # 状态
ssh -F ssh.config Azure-Server "sudo systemctl restart tidewatch"  # 重启
ssh -F ssh.config Azure-Server "cd ~/GitHub_Workspace/TideWatch-MCP-Server && git pull && sudo systemctl restart tidewatch"  # 更新部署
```

### 本地模式 (stdio)
```bash
poetry run tidewatch            # Claude Desktop / Cursor / VS Code
```

### 远程模式 (HTTP)
```bash
# Azure VM 上运行
poetry run tidewatch --http --port 8889

# 或用 systemd
sudo cp scripts/tidewatch.service /etc/systemd/system/
sudo systemctl enable --now tidewatch
```

### 架构
```
客户端 (VS Code / Claude Desktop)
    │
    │ HTTPS + API Key (X-API-Key header)
    ▼
tidewatch.polly.wang:443 (Nginx + Let's Encrypt SSL)
    │
    │ proxy_pass (HTTP)
    ▼
127.0.0.1:8889 (FastMCP Streamable HTTP)
```

### 客户端配置
```json
{
    "TideWatch": {
        "url": "https://tidewatch.polly.wang/mcp",
        "headers": { "X-API-Key": "polly-tidewatch-xxx" }
    }
}
```

### 部署步骤
1. Cloudflare 加 A 记录: `tidewatch` → Azure VM IP
2. Azure VM 上 `git clone` + `./setup.sh`
3. 编辑 `.env` 设置 `COPILOTX_API_KEY`（`MCP_API_KEY` 由 setup.sh 自动生成）
4. `sudo ./scripts/setup_domain.sh` (配置 Nginx + SSL)
5. `sudo systemctl enable --now tidewatch`

## Known Issues

- `stock_zh_a_spot_em()` 在本地 Mac 和 Azure VM 上均无法使用 — 根因是东方财富 `push2.eastmoney.com` 对非浏览器请求做了反爬限制（SSL 握手成功但返回 Empty reply），与 DNS 和地域无关。影响范围：`scan_market`、`get_stock_realtime`、`get_stock_name`（已有 fallback）。其他 AKShare 接口（日K线 `stock_zh_a_hist`、资金流向、新闻等）正常
- MCP 工具不要加 `dict[str, Any]` 返回类型注解（FastMCP 2.x outputSchema 冲突）
- 日志必须输出到 stderr（MCP 用 stdout 通信）
- 信号记录已加当天去重：同一 symbol + 同一 score 当天内不重复入库（前端额外用 `date|symbol|score` key 去重展示）
- 时间戳均使用北京时间 (UTC+8)，通过 `_now_bj()` 统一处理，Azure VM 默认 UTC
- `analyze_stock` 股票名称解析链：持仓名称 → 自选名称 → HOT_NAMES → get_stock_name()，避免 push2 失效时显示代码
- 后台预热仅在北京时间 7:00-23:59 执行，凌晨 0-7 点跳过（东方财富维护窗口断连）
- 扫描缓存持久化到 `data/scan_cache.json`，重启后自动恢复，避免冷启动 Dashboard 显示空数据
- Azure VM 并发 58 只股票拉 AKShare 会触发东方财富限流/断连，单个 analyze_stock 正常，ThreadPoolExecutor 已加 120s/10s 双层超时。HOT_POOL 已精简至 24 只，解决此问题
- `analyze_stock` 通过 `asyncio.to_thread()` 包装，不阻塞事件循环
- `scan_market` 同样通过 `asyncio.to_thread(_scan_market_sync)` 包装 — async def 里不能做同步阻塞 I/O（~27只×0.3s=~8s 会卡死整个事件循环）
- 日K线数据源已迁移至 baostock（单只 0.28s，零反爬），AKShare 仅用于资金流向/新闻/龙虎榜/北向/ETF
- baostock 单连接 + `threading.Lock(acquire timeout=15s)` 保护线程安全 + 30s 自动重连
- baostock socket 三层超时保护（🦞9.0/10）：monkey-patch `connect()` 注入 10s `settimeout` → `_bs_login()` 登录后双保险 `settimeout` → 异常统一走 `_force_close_bs_socket()` 关 socket + 标记 session 失效。根治僵尸 TCP 卡死进程问题
- Dashboard 自动刷新仅在盘中 + 可见标签 + 无详情面板时触发（智能三重守卫）
- **数据库在远程 Azure VM 上** — `data/signals.db` 本地仅空库，排查持仓/自选/信号问题必须 SSH 到 Azure VM 查询
- scan_market 级联失败保护（3+ A 股连续失败 → 暂停重连 baostock）+ 持仓/自选 A 股全缺失时末尾重试一轮，避免瞬时故障导致关键股票丢失
- 回填 K 线日期去重 — baostock 偶发返回重复日期行，`drop_duplicates(subset=["date"])` 双层防护（data.py + tracker.py），10d/20d 增加日历天安全阀（14/28天）
- scan_market 轻量冲突检测 — 用 OBV 斜率代替 AKShare 资金流向（零额外网络请求），5 种冲突类型（技术vs资金/个股vs大盘/放量下跌/缩量上涨），Dashboard 卡片直接渲染金色框 + 琥珀色冲突摘要文字
