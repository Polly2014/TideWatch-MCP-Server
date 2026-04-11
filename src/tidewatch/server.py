"""
观潮 (TideWatch) — MCP Server
AI 投研搭档，多维融合股票分析引擎

Supports two transport modes:
  - stdio:  For local Claude Desktop / Cursor integration (default)
  - http:   For remote HTTP access (Streamable HTTP)

Usage:
    # stdio mode (default, for local use)
    poetry run tidewatch

    # HTTP mode (for remote access behind Nginx)
    poetry run tidewatch --http --port 8889

    # Or with uvicorn directly
    uvicorn tidewatch.server:http_app --host 0.0.0.0 --port 8889

Client Configuration:
    {
        "url": "https://tidewatch.polly.wang/mcp",
        "headers": {"X-API-Key": "your-api-key"}
    }

MCP Tools:
  - analyze_stock     个股综合分析（技术面+资金面+消息面）
  - get_regime        市场体制识别（牛/熊/横盘/高波动）
  - scan_signals      全市场扫描强势/弱势信号
  - compare_stocks    多股横向对比
  - get_money_flow    资金流向分析
  - get_stock_news    个股相关新闻
"""

import asyncio
import argparse
import concurrent.futures
import logging
import os
import sys
import time as _time
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path

# 北京时间 UTC+8
_BJ_TZ = timezone(timedelta(hours=8))

def _now_bj():
    return datetime.now(_BJ_TZ)

# analyze_stock 盘后缓存：{"symbol:YYYY-MM-DD": report}，同一股票同一交易日盘后只算一次
_analyze_cache: dict = {}
_analyze_cache_date: str = ""  # 当前缓存的交易日，新交易日自动清空
from typing import Any

import pandas as pd
from dotenv import load_dotenv
from fastmcp import FastMCP

from .data import MarketData, is_us_stock
from .guardrails import check_guardrails
from .llm import polish_narrative
from .narrative import NarrativeGenerator
from .portfolio import (
    add_holding, remove_holding, get_holdings,
    add_watchlist, remove_watchlist, get_watchlist,
    set_account_info, get_account_info,
    get_scan_pool, HOT_POOL, HOT_NAMES,
)
from .regime import RegimeDetector
from .technical import TechnicalAnalyzer
from .tracker import record_signal, get_recent_signals, get_signal_stats, update_outcomes

# ============================================================================
# 配置加载
# ============================================================================

PROJECT_ROOT = Path(__file__).parent.parent.parent

config_path = PROJECT_ROOT / "config.env"
if config_path.exists():
    load_dotenv(config_path)
# 也加载 .env（优先级更高，可覆盖 config.env 的值）
env_path = PROJECT_ROOT / ".env"
if env_path.exists():
    load_dotenv(env_path, override=True)

# AKShare 通过东方财富 API 获取数据，代理会导致连接超时
# VS Code 可能注入代理变量，macOS 还有系统级代理
# NO_PROXY=* 告诉 requests 库对所有主机绕过代理
for proxy_var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(proxy_var, None)
os.environ["NO_PROXY"] = "*"
os.environ["no_proxy"] = "*"

# 日志配置 — MCP 使用 stdout 通信，日志必须输出到 stderr
log_path = PROJECT_ROOT / "data" / "tidewatch.log"
log_path.parent.mkdir(exist_ok=True)
logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO")),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(log_path),
        logging.StreamHandler(sys.stderr),
    ],
)
logger = logging.getLogger(__name__)

# ============================================================================
# MCP 服务器初始化
# ============================================================================

mcp = FastMCP("TideWatch-观潮")

# 核心组件（全局单例）
market_data = MarketData()
technical = TechnicalAnalyzer()
regime_detector = RegimeDetector()
narrator = NarrativeGenerator()

# HTTP 远程部署配置
MCP_API_KEY = os.getenv("MCP_API_KEY", "")
MCP_API_KEY_ENABLED = bool(MCP_API_KEY)

VERSION = "0.3.0"

# 服务器统计
server_stats = {
    "start_time": _now_bj().isoformat(),
    "analyses_completed": 0,
    "scans_completed": 0,
}

# scan_market 缓存（5分钟内复用上次结果，避免频繁请求被封）
_scan_cache = {"result": None, "time": 0}
_SCAN_CACHE_TTL = 300  # 5分钟
_warmup_done = threading.Event()  # 预热完成标志
_scan_bg_refreshing = False  # 后台刷新中标志，防止并发刷新


# ─── 后台预热 & 定时刷新 ─────────────────────────────────

def _is_market_hours():
    """判断是否在 A 股交易时段（工作日 9:15-15:05，北京时间）"""
    now = _now_bj()
    if now.weekday() >= 5:  # 周末
        return False
    hhmm = now.hour * 100 + now.minute
    return 915 <= hhmm <= 1505

def _warmup_loop():
    """后台线程：启动预热 + 盘中定时刷新 scan_market 缓存"""
    _time.sleep(3)  # 等服务就绪

    # 只在凌晨 0-7 点（东方财富维护窗口）跳过预热，其他时间都执行
    bj_hour = _now_bj().hour
    if bj_hour >= 7:  # 北京时间 7:00 后都可以预热（包括周末）
        logger.info("🔥 后台预热: 首次 scan_market 开始...")
        try:
            _run_scan_warmup()
            logger.info("🔥 后台预热: 首次 scan_market 完成，缓存已就绪")
        except Exception as e:
            logger.error(f"🔥 后台预热失败: {e}")
    else:
        logger.info(f"🔥 凌晨 {bj_hour}:xx，跳过首次预热（AKShare 维护窗口）")
    _warmup_done.set()  # 无论成功失败都标记完成

    # 定时刷新循环
    while True:
        _time.sleep(_SCAN_CACHE_TTL)  # 每 5 分钟检查一次

        # 💓 baostock 心跳检测（每轮都做，不限盘中）
        try:
            from .data import bs_heartbeat
            if not bs_heartbeat():
                logger.warning("💓 baostock 心跳异常，已尝试重连")
        except Exception as e:
            logger.error(f"💓 baostock 心跳检测异常: {e}")

        if _is_market_hours():
            try:
                logger.info("🔄 定时刷新: scan_market 缓存更新中...")
                _run_scan_warmup()
                logger.info("🔄 定时刷新: scan_market 缓存已更新")
            except Exception as e:
                logger.error(f"🔄 定时刷新失败: {e}")

def _run_scan_warmup():
    """扫描核心逻辑（唯一实现）— 填充 _scan_cache + 持久化到磁盘"""
    pool = get_scan_pool()
    holdings_info = {h["symbol"]: h for h in get_holdings()}
    watchlist_info = {w["symbol"]: w for w in get_watchlist()}

    # A股/美股分别拉一次体制，各自共用（避免重复拉指数）
    _regime_biases = {}  # {"A": bias, "US": bias}
    _regime_names = {}   # {"A": "mild_bear", "US": "bull"}
    for market_key, idx_code in [("A", "000001"), ("US", "SPY")]:
        try:
            idx_df = market_data.get_index_daily(idx_code, days=120)
            r = regime_detector.detect(idx_df)
            _regime_biases[market_key] = regime_detector.get_regime_adjustment(r["regime"])["signal_bias"]
            _regime_names[market_key] = r["regime"]
        except Exception:
            _regime_biases[market_key] = 0
            _regime_names[market_key] = ""

    def _score_one(code):
        is_us = is_us_stock(str(code))
        market_key = "US" if is_us else "A"
        regime_bias = _regime_biases[market_key]
        regime_name = _regime_names[market_key]
        try:
            daily = market_data.get_stock_daily(str(code), days=60)
            if daily.empty or len(daily) < 20:
                return None
            tech_result = technical.analyze(daily)
            if "error" in tech_result:
                return None
            close_col = "close" if "close" in daily.columns else "收盘"
            latest_price = float(daily.iloc[-1].get(close_col, 0))
            pct_col = "pct_change" if "pct_change" in daily.columns else "涨跌幅"
            pct_today = float(daily.iloc[-1].get(pct_col, 0))
            stored_name = holdings_info.get(code, {}).get("name", "")
            watchlist_name = watchlist_info.get(code, {}).get("name", "")
            if stored_name and stored_name != code:
                name = stored_name
            elif watchlist_name and watchlist_name != code:
                name = watchlist_name
            elif code in HOT_NAMES:
                name = HOT_NAMES[code]
            else:
                name = market_data.get_stock_name(str(code))
            raw_score = tech_result["trend"]["score"]
            adjusted = max(-100, min(100, raw_score + regime_bias))
            # v3 信号阈值（≥50 才看多, [+8,+50) 全中性, mild_bear 下 |score|<50 全中性）
            if adjusted >= 50:
                sig = "中性观望" if regime_name == "mild_bear" else "看多"
            elif adjusted >= 8: sig = "中性观望"
            elif adjusted <= -25: sig = "看空"
            elif adjusted <= -8:
                sig = "中性观望" if regime_name == "mild_bear" else "偏空"
            else: sig = "中性观望"

            # 轻量冲突检测（用 OBV 斜率代替资金流向，零额外网络请求）
            scan_conflicts = []
            trend_score = tech_result["trend"]["score"]
            vol = tech_result.get("volume", {})
            obv_slope = vol.get("obv_slope", 0)
            pct_5d = tech_result.get("price_position", {}).get("pct_5d", 0)
            if trend_score > 10 and obv_slope < -0.02:
                scan_conflicts.append({
                    "type": "tech_vs_money",
                    "description": "⚠️ 技术面偏多但主力资金在流出，可能是诱多",
                })
            if trend_score < -10 and obv_slope > 0.02:
                scan_conflicts.append({
                    "type": "tech_vs_money",
                    "description": "📋 技术面偏空但主力在吸筹，可能是洗盘",
                })
            if trend_score > 20 and regime_name in ("bear", "mild_bear"):
                scan_conflicts.append({
                    "type": "stock_vs_market",
                    "description": "📉 个股技术面强但大盘偏弱，逆势走强需关注持续性",
                })
            if vol.get("expanding") and pct_5d < -3:
                scan_conflicts.append({
                    "type": "volume_price",
                    "description": "🚨 放量下跌，可能是主力出逃信号",
                })
            if vol.get("shrinking") and pct_5d > 3:
                scan_conflicts.append({
                    "type": "volume_price",
                    "description": "📊 缩量上涨，上行动能不足",
                })

            result = {
                "code": str(code), "name": name,
                "price": latest_price, "pct_today": pct_today,
                "score": adjusted,
                "signal": sig,
                "rsi": tech_result["momentum"]["rsi_14"],
                "reasons_bull": tech_result["trend"]["reasons_bull"][:3],
                "reasons_bear": tech_result["trend"]["reasons_bear"][:3],
            }
            if scan_conflicts:
                result["conflicts"] = scan_conflicts
            result["sparkline"] = [round(float(x), 2) for x in daily[close_col].tail(7).tolist()]
            if code in holdings_info:
                h = holdings_info[code]
                result["added_at"] = h.get("added_at", "")
                if h.get("cost") and h["cost"] > 0:
                    result["cost"] = h["cost"]
                    result["shares"] = h.get("shares", 0)
                    result["pnl_pct"] = round((latest_price - h["cost"]) / h["cost"] * 100, 2)
                    result["pnl_amount"] = round((latest_price - h["cost"]) * h.get("shares", 0), 2)
            return result
        except Exception as e:
            logger.debug(f"扫描 {code} 失败: {e}")
            return None

    all_symbols = pool["holdings"] + pool["watchlist"] + pool["hot"]
    results = {}
    consecutive_failures = 0

    # baostock 是单连接串行，不用 ThreadPoolExecutor（更快更稳）
    for sym in all_symbols:
        try:
            r = _score_one(sym)
            if r:
                results[sym] = r
                consecutive_failures = 0
            else:
                if not is_us_stock(str(sym)):
                    consecutive_failures += 1
        except Exception:
            if not is_us_stock(str(sym)):
                consecutive_failures += 1

        # 级联失败检测：连续 3+ A 股失败 → 暂停重连 baostock
        if consecutive_failures >= 3:
            logger.warning(f"⚠️ 扫描级联失败: 连续 {consecutive_failures} 只 A 股失败，强制重连 baostock")
            try:
                from .data import _force_close_bs_socket, _bs_login, _bs_lock
                if _bs_lock.acquire(timeout=10):
                    try:
                        _force_close_bs_socket()
                        _time.sleep(1)
                        _bs_login()
                        logger.info("⚠️ baostock 重连成功，继续扫描")
                    finally:
                        _bs_lock.release()
            except Exception as e:
                logger.error(f"⚠️ baostock 重连失败: {e}")
            consecutive_failures = 0

        _time.sleep(0.05)  # 让出锁给 analyze_stock 请求

    # 持仓/自选末尾重试：只要有任一关键 A 股缺失就重连后补一轮
    critical_symbols = pool["holdings"] + pool["watchlist"]
    missing_critical = [s for s in critical_symbols if s not in results and not is_us_stock(str(s))]
    if missing_critical:
        logger.warning(f"🔄 {len(missing_critical)} 只关键 A 股缺失({missing_critical})，末尾重试")
        try:
            from .data import _force_close_bs_socket, _bs_login, _bs_lock
            if _bs_lock.acquire(timeout=10):
                try:
                    _force_close_bs_socket()
                    _time.sleep(2)
                    _bs_login()
                finally:
                    _bs_lock.release()
            for sym in missing_critical:
                try:
                    r = _score_one(sym)
                    if r:
                        results[sym] = r
                        logger.info(f"🔄 重试成功: {sym}")
                except Exception:
                    pass
                _time.sleep(0.05)
        except Exception as e:
            logger.error(f"🔄 末尾重试失败: {e}")

    # 重试后仍有缺失则告警
    still_missing = [s for s in critical_symbols if s not in results]
    if still_missing:
        logger.error(f"❌ 扫描完成但仍有 {len(still_missing)} 只关键股票缺失: {still_missing}")

    holding_results = sorted([results[s] for s in pool["holdings"] if s in results], key=lambda x: x["score"], reverse=True)
    watchlist_results = sorted([results[s] for s in pool["watchlist"] if s in results], key=lambda x: x["score"], reverse=True)
    hot_results = sorted([results[s] for s in pool["hot"] if s in results], key=lambda x: x["score"], reverse=True)

    # 持仓额外上下文：技术面弱但仍在盈利区时提示
    for h in holding_results:
        if h.get("pnl_pct") is not None and h["pnl_pct"] > 0 and h["score"] < -10:
            h["context"] = "技术面弱但仍在盈利区，可考虑设置移动止盈"
        elif h.get("pnl_pct") is not None and h["pnl_pct"] < -5 and h["score"] < -20:
            h["context"] = "技术面+亏损双重压力，关注止损位"

    # 自选为空时加引导提示
    watchlist_hint = ""
    if not watchlist_results and not pool["watchlist"]:
        watchlist_hint = "自选池为空，用 manage_watchlist(action='add', symbol='xxx') 添加关注股票"

    scan_result = {
        "holdings": holding_results,
        "watchlist": watchlist_results,
        "watchlist_hint": watchlist_hint,
        "_hot_sorted": hot_results,
        "hot_strongest": hot_results[:10],
        "hot_weakest": sorted(hot_results[-10:], key=lambda x: x["score"]) if len(hot_results) > 10 else [],
        "account": get_account_info(),
        "pool_size": {
            "holdings": len(pool["holdings"]),
            "watchlist": len(pool["watchlist"]),
            "hot": len(pool["hot"]),
            "total": len(all_symbols),
            "scanned": len(results),
        },
        "timestamp": _now_bj().isoformat(),
    }
    # 残缺缓存覆盖保护：成功率过低时不覆盖旧缓存
    # TODO: 0.5 阈值基于当前 ~32 只池子，若池子缩至 <10 只需调高
    scan_ratio = len(results) / len(all_symbols) if all_symbols else 0
    if scan_ratio < 0.5 and _scan_cache["result"]:
        logger.warning(f"⚠️ 扫描成功率过低 ({len(results)}/{len(all_symbols)} = {scan_ratio:.0%})，保留旧缓存")
        return

    _scan_cache["result"] = scan_result
    _scan_cache["time"] = _time.monotonic()

    # 持久化到磁盘（重启后可恢复）
    if len(results) > 0:
        try:
            import json
            cache_path = Path(__file__).parent.parent.parent / "data" / "scan_cache.json"
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(scan_result, ensure_ascii=False, default=str))
            logger.info(f"💾 扫描缓存已持久化: {len(results)} 只股票")
        except Exception as e:
            logger.debug(f"缓存持久化失败: {e}")


def _load_disk_cache():
    """启动时从磁盘恢复上次扫描缓存"""
    try:
        import json
        cache_path = Path(__file__).parent.parent.parent / "data" / "scan_cache.json"
        if cache_path.exists():
            data = json.loads(cache_path.read_text())
            _scan_cache["result"] = data
            _scan_cache["time"] = _time.monotonic()  # 标记为刚缓存
            logger.info(f"💾 从磁盘恢复扫描缓存: {data.get('pool_size', {}).get('scanned', '?')} 只股票")
            return True
    except Exception as e:
        logger.debug(f"磁盘缓存恢复失败: {e}")
    return False

# 启动时先尝试从磁盘恢复缓存
_load_disk_cache()

# 启动后台预热线程（daemon=True 跟随主进程退出）
_warmup_thread = threading.Thread(target=_warmup_loop, daemon=True)
_warmup_thread.start()


# ============================================================================
# MCP Tools
# ============================================================================


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request):
    """Health check endpoint (no auth required)"""
    from starlette.responses import JSONResponse
    tools = await mcp.list_tools()
    return JSONResponse({
        "status": "healthy",
        "server": "TideWatch-观潮",
        "version": VERSION,
        "transport": "streamable-http",
        "tools_count": len(tools),
        "auth_enabled": MCP_API_KEY_ENABLED,
        "analyses_completed": server_stats["analyses_completed"],
    })


@mcp.tool()
async def analyze_stock(
    symbol: str,
    include_news: bool = True,
    include_money_flow: bool = True,
    days: int = 120,
    skip_llm: bool = False,
):
    """
    个股综合分析 — 技术面 + 资金面 + 消息面 + 市场体制

    这是观潮的核心分析工具，对一只股票进行多维度交叉分析。
    不只看技术指标，还会结合当前大盘体制、资金流向、新闻消息，
    输出带有冲突检测的综合研判。

    Args:
        symbol: 股票代码（A股纯数字如 "002111"，美股字母如 "AAPL"）
        include_news: 是否包含新闻消息面分析（美股暂不支持）
        include_money_flow: 是否包含资金流向（美股暂不支持）
        days: K线天数（默认120个交易日）
        skip_llm: 跳过 LLM 叙事润色（Dashboard 快速模式用，先返回模板叙事）

    Returns:
        综合分析报告，包含技术面、资金面、消息面、市场体制、冲突检测
    """
    # 防御校验：A股6位纯数字 / 美股1-5位字母（拦截误传的时间戳微秒等垃圾输入）
    symbol = symbol.strip()
    if is_us_stock(symbol):
        if not symbol.isalpha() or len(symbol) > 5:
            return {"error": f"无效的美股代码: {symbol}"}
    else:
        if not symbol.isdigit() or len(symbol) != 6:
            return {"error": f"无效的A股代码: {symbol}（应为6位数字）"}

    logger.info(f"📊 开始分析: {symbol}")
    return await asyncio.to_thread(_analyze_stock_sync, symbol, include_news, include_money_flow, days, skip_llm)


def _analyze_stock_sync(symbol, include_news, include_money_flow, days, skip_llm):
    global _analyze_cache, _analyze_cache_date
    t0 = _time.monotonic()

    # 盘后缓存：A股 15:05+ 或美股非交易时段，同一股票同一交易日复用上次结果
    now = _now_bj()
    today_str = now.strftime("%Y-%m-%d")
    is_market_closed = now.hour >= 15 or now.hour < 9 or now.weekday() >= 5
    cache_key = f"{symbol}:{today_str}"

    # 新交易日清空旧缓存
    if _analyze_cache_date != today_str:
        _analyze_cache.clear()
        _analyze_cache_date = today_str

    # 盘后 + 非 skip_llm + 缓存命中 → 直接返回
    if is_market_closed and not skip_llm and cache_key in _analyze_cache:
        logger.info(f"📦 {symbol} 盘后缓存命中，0 LLM 调用")
        return _analyze_cache[cache_key]
    # baostock 每次查询都 auto-reconnect，无需等预热

    # ETF 检测（纯前缀判断，无网络请求）
    is_etf = market_data._is_etf(symbol)
    _is_us = is_us_stock(symbol)

    # 并发拉取数据源（K线 + 指数 + 资金 + 新闻）
    # 美股：使用 SPY 作为基准指数，跳过 A 股独有的资金流向
    index_code = "SPY" if _is_us else "000001"
    _skip_money = _is_us or is_etf or not include_money_flow
    _skip_news = is_etf or not include_news  # 美股也拉新闻（yfinance）
    _skip_lhb = _is_us or is_etf  # 龙虎榜仅 A 股非 ETF
    # 不用 with（shutdown(wait=True) 会等所有 future，AKShare 挂住会拖死整个分析）
    _executor = concurrent.futures.ThreadPoolExecutor(max_workers=5)
    f_daily = _executor.submit(market_data.get_stock_daily, symbol, days)
    f_index = _executor.submit(market_data.get_index_daily, index_code, days)
    f_money = _executor.submit(market_data.get_money_flow, symbol) if not _skip_money else None
    f_news = _executor.submit(market_data.get_stock_news, symbol, 5) if not _skip_news else None
    f_lhb = _executor.submit(market_data.get_lhb, symbol) if not _skip_lhb else None
    _executor.shutdown(wait=False)  # 不阻塞，各 future 独立完成

    # 1. 日K线（核心数据源，必须成功）— baostock ~0.3s
    df = f_daily.result(timeout=30)
    t1 = _time.monotonic()
    logger.info(f"⏱️ {symbol} 数据拉取: {t1-t0:.1f}s")
    if df.empty:
        return {"error": f"无法获取 {symbol} 的行情数据"}

    # 2. 技术分析（基于日K线）
    tech = technical.analyze(df)
    if "error" in tech:
        return tech

    # 股票名称（持仓/自选 → HOT_NAMES → get_stock_name）
    _holdings = get_holdings()
    _wl = get_watchlist()
    _h_name = next((h.get("name", "") for h in _holdings if h["symbol"] == symbol), "")
    _w_name = next((w.get("name", "") for w in _wl if w["symbol"] == symbol), "")
    stock_name = _h_name or _w_name or HOT_NAMES.get(symbol) or market_data.get_stock_name(symbol)

    # 实时行情：直接用日K线最后一条（避免 stock_zh_a_spot_em 全市场爬取 60-90s）
    # PE/PB 也从 K 线取（baostock peTTM/pbMRQ，每日更新，零额外请求）
    last = df.iloc[-1]
    pe_val = float(last.get("pe_ttm", 0)) if pd.notna(last.get("pe_ttm")) else 0
    pb_val = float(last.get("pb_mrq", 0)) if pd.notna(last.get("pb_mrq")) else 0
    realtime = {
        "price": float(last["close"]),
        "pct_change": float(last.get("pct_change", 0)),
        "pe": round(pe_val, 2), "pb": round(pb_val, 2), "total_mv": 0,
    }

    # 3. 市场体制（已并发拉取）— baostock ~0.3s
    index_df = f_index.result(timeout=30)
    regime_result = regime_detector.detect(index_df)
    regime_adj = regime_detector.get_regime_adjustment(regime_result["regime"])

    # 4. 资金面（已并发拉取）/ 美股用 SPY 相对强弱替代 — AKShare 15s 超时
    money = {}
    if f_money:
        try:
            money = f_money.result(timeout=15)
        except (concurrent.futures.TimeoutError, Exception) as e:
            logger.warning(f"⏱️ {symbol} 资金流向超时/失败: {e}")
            money = {}
    if _is_us and not money and not index_df.empty and len(index_df) >= 5:
        spy_pct_5d = (float(index_df.iloc[-1]["close"]) / float(index_df.iloc[-5]["close"]) - 1) * 100
        stock_pct_5d = tech.get("price_position", {}).get("pct_5d", 0)
        money["_us_relative"] = {
            "spy_pct_5d": round(spy_pct_5d, 2),
            "stock_pct_5d": round(stock_pct_5d, 2),
            "relative": round(stock_pct_5d - spy_pct_5d, 2),
        }

    # 5. 消息面（已并发拉取）— AKShare/yfinance 15s 超时
    news = []
    if f_news:
        try:
            news = f_news.result(timeout=15)
        except (concurrent.futures.TimeoutError, Exception) as e:
            logger.warning(f"⏱️ {symbol} 新闻超时/失败: {e}")
            news = []

    # 6. 龙虎榜（已并发拉取，仅 A 股非 ETF）— AKShare 15s 超时
    lhb = []
    if f_lhb:
        try:
            lhb = f_lhb.result(timeout=15)
        except (concurrent.futures.TimeoutError, Exception) as e:
            logger.warning(f"⏱️ {symbol} 龙虎榜超时/失败: {e}")
            lhb = []

    t2 = _time.monotonic()
    logger.info(f"⏱️ {symbol} 分析+体制+资金+新闻: {t2-t1:.1f}s (总 {t2-t0:.1f}s)")

    # 6. 冲突检测
    conflicts = _detect_conflicts(tech, money, regime_result)

    # 7. 综合评分（技术面得分 + 体制调整）
    raw_score = tech["trend"]["score"]
    adjusted_score = raw_score + regime_adj["signal_bias"]
    adjusted_score = max(-100, min(100, adjusted_score))

    # 8. 最终信号（v3: ≥+50 才看多, [+8,+50) 全部中性, mild_bear 下 |score|<50 全中性, 基于 91 条回填数据）
    regime_name = regime_result.get("regime", "")
    if adjusted_score >= 50:
        # mild_bear 下 score<50 全中性（v3 扩展：mild_bear 57.1% 胜率，弱信号不可信）
        if regime_name == "mild_bear":
            final_signal = "中性观望"
        else:
            final_signal = "看多"
    elif adjusted_score >= 8:
        # v3: [+8,+50) 全部中性观望（回填 11 条胜率仅 27.3%）
        final_signal = "中性观望"
    elif adjusted_score <= -25:
        final_signal = "看空"
    elif adjusted_score <= -8:
        # mild_bear 下弱空也不可信
        if regime_name == "mild_bear":
            final_signal = "中性观望"
        else:
            final_signal = "偏空"
    else:
        final_signal = "中性观望"

    server_stats["analyses_completed"] += 1

    # 构建持仓上下文（供 LLM 个性化分析）
    _holding = next((h for h in _holdings if h["symbol"] == symbol), None)
    _watching = next((w for w in _wl if w["symbol"] == symbol), None)
    portfolio_ctx = ""
    if _holding:
        cost = _holding.get("cost", 0)
        shares = _holding.get("shares", 0)
        price = realtime.get("price", 0)
        if cost and cost > 0 and shares > 0:
            pnl_pct = round((price - cost) / cost * 100, 2)
            pnl_amt = round((price - cost) * shares, 0)
            if _is_us:
                portfolio_ctx = f"用户持仓: {shares}股，成本价${cost:.2f}，当前浮{'\u76c8' if pnl_pct >= 0 else '\u4e8f'}{abs(pnl_pct):.1f}%（{'+'if pnl_amt>=0 else ''}{pnl_amt:.0f}美元）"
            else:
                lots = shares // 100  # A股1手=100股
                lot_note = f"（仅{lots}手，已是最小持仓单位，无法减半）" if lots <= 1 else f"（{lots}手）"
                portfolio_ctx = f"用户持仓: {shares}股{lot_note}，成本价¥{cost:.2f}，当前浮{'\u76c8' if pnl_pct >= 0 else '\u4e8f'}{abs(pnl_pct):.1f}%（{'+'if pnl_amt>=0 else ''}{pnl_amt:.0f}元）"
        else:
            portfolio_ctx = f"用户持仓: {shares}股"
    elif _watching:
        reason = _watching.get("reason", "")
        portfolio_ctx = f"用户已加入自选股" + (f"，关注原因: {reason}" if reason else "")
    else:
        portfolio_ctx = "用户未持仓，仅在浏览"

    # 追加可用资金上下文（仅 A 股，美股账户是人民币无参考价值）
    _acct = get_account_info()
    if _acct["cash"] > 0 and not _is_us:
        portfolio_ctx += f"\n账户可用资金: ¥{_acct['cash']:,.2f}，总资产: ¥{_acct['total_assets']:,.2f}"

    report = {
        "stock": {
            "code": symbol,
            "name": stock_name,
            "price": realtime.get("price", tech["price_position"]["price"]),
            "pct_change": realtime.get("pct_change", 0),
            "pe": realtime.get("pe", 0),
            "pb": realtime.get("pb", 0),
            "total_mv": realtime.get("total_mv", 0),
        },
        "signal": {
            "direction": final_signal,
            "raw_score": raw_score,
            "adjusted_score": adjusted_score,
            "confidence": _calc_confidence(adjusted_score, symbol),
            "regime_adjustment": regime_adj["signal_bias"],
        },
        "technical": tech,
        "regime": regime_result,
        "money_flow": money,
        "news": news,
        "lhb": lhb,
        "conflicts": conflicts,
        "advice": {
            "position_max": f"{regime_adj['position_max'] * 100:.0f}%",
            "stop_loss_hint": f"ATR止损建议: 价格 - {tech['volatility']['atr_14'] * regime_adj['stop_loss_multiplier']:.2f}",
        },
        "narrative": narrator.generate(stock_name, tech, regime_result, money, conflicts, final_signal),
        "portfolio_context": portfolio_ctx,
        "timestamp": _now_bj().isoformat(),
    }

    # 9. LLM 叙事润色（可选，失败时保留模板叙事）
    if not skip_llm:
        # 构建结构化数据摘要供 LLM 独立判断
        _vol = tech.get("volume", {})
        _mom = tech.get("momentum", {})
        _ma = tech.get("ma", {})
        _pos = tech.get("price_position", {})
        _boll = tech.get("volatility", {})
        summary_lines = [
            f"方向: {final_signal} | 综合评分: {adjusted_score:+d} (原始{raw_score:+d}, 体制调整{regime_adj['signal_bias']:+d})",
            f"价格: {realtime['price']:.2f} | 5日涨跌: {_pos.get('pct_5d', 0):+.1f}% | 20日位置: {_pos.get('position_20d', 50):.0f}%",
            f"均线: {'多头排列' if _ma.get('bullish_aligned') else '空头排列' if _ma.get('bearish_aligned') else '交织'} | MA5偏离: {_ma.get('price_vs_ma5', 0):+.1f}% | MA20偏离: {_ma.get('price_vs_ma20', 0):+.1f}%",
            f"动量: RSI {_mom.get('rsi_14', 50):.0f} | MACD {_mom.get('macd_cross', '无')}",
            f"量能: 量比 {_vol.get('volume_ratio', 1):.1f}x | OBV斜率 {_vol.get('obv_slope', 0):.3f} | 换手率 {_vol.get('turn_rate', 0):.1f}% (5日均 {_vol.get('avg_turn_5d', 0):.1f}%)",
        ]
        if realtime.get("pe", 0) > 0:
            summary_lines.append(f"估值: PE(TTM) {realtime['pe']:.1f} | PB(MRQ) {realtime['pb']:.2f}")
        summary_lines.append(f"布林: 位置 {_boll.get('boll_position', 50):.0f}% | ATR {_boll.get('atr_14', 0):.2f}")
        summary_lines.append(f"体制: {regime_result.get('description', '')} | 建议仓位 ≤{regime_adj['position_max'] * 100:.0f}%")
        if conflicts:
            conflict_descs = [c.get("description", "") for c in conflicts]
            summary_lines.append(f"冲突: {' | '.join(conflict_descs)}")
        data_summary = "\n".join(summary_lines)

        try:
            report["narrative"] = polish_narrative(
                report["narrative"], stock_name, adjusted_score,
                portfolio_context=portfolio_ctx,
                is_us=_is_us,
                news=news,
                data_summary=data_summary,
            )
        except Exception as e:
            logger.debug(f"LLM 润色跳过: {e}")

    # 10. 行为护栏检测
    try:
        guardrail_warnings = check_guardrails(symbol, tech, score=adjusted_score, conflicts=conflicts)
        if guardrail_warnings:
            report["guardrails"] = guardrail_warnings
            # 护栏警告也加入叙事末尾
            guardrail_text = "\n\n".join(
                f"{w['message']} {w['advice']}" for w in guardrail_warnings
            )
            report["narrative"] += "\n\n" + guardrail_text
    except Exception as e:
        logger.warning(f"行为护栏检测失败: {e}")

    # 11. 记录信号到追踪系统（仅完整分析，skip_llm 模式不写信号 — 防止冒烟测试 / Dashboard 点击污染信号池）
    if not skip_llm:
      try:
        signal_id = record_signal(
            symbol=symbol,
            name=stock_name,
            score=adjusted_score,
            direction=final_signal,
            price=report["stock"]["price"],
            regime=regime_result.get("regime", "unknown"),
            confidence=tech["trend"]["confidence"],
            reasons_bull=tech["trend"].get("reasons_bull", []),
            reasons_bear=tech["trend"].get("reasons_bear", []),
            conflicts=conflicts,
        )
        report["signal"]["tracked_id"] = signal_id
      except Exception as e:
        logger.warning(f"信号记录失败: {e}")

    t3 = _time.monotonic()
    logger.info(f"⏱️ {symbol} 分析完成: 总耗时 {t3-t0:.1f}s (数据{t1-t0:.1f}s + 分析{t2-t1:.1f}s + 其他{t3-t2:.1f}s)")

    # 盘后写入缓存（仅完整分析，非 skip_llm）
    if is_market_closed and not skip_llm:
        _analyze_cache[cache_key] = report
        logger.info(f"📦 {symbol} 盘后缓存已写入")

    return report


@mcp.tool()
async def polish_narrative_llm(
    template_narrative: str,
    stock_name: str,
    score: int,
    portfolio_context: str = "",
    news_headlines: str = "",
):
    """
    LLM 叙事润色 — 将模板叙事润色为自然的分析师语气

    配合 analyze_stock(skip_llm=True) 使用，实现渐进式加载：
    先用模板叙事秒出结果，再异步调用此工具润色。

    Args:
        template_narrative: analyze_stock 返回的模板叙事文本
        stock_name: 股票名称
        score: 综合评分（adjusted_score）
        portfolio_context: 用户持仓上下文
        news_headlines: 新闻标题（换行分隔，由前端从 analyze_stock 结果拼接）

    Returns:
        润色后的叙事文本
    """
    # 将新闻标题字符串还原为 news list
    news = []
    if news_headlines:
        news = [{"title": t.strip()} for t in news_headlines.split("\n") if t.strip()]
    try:
        polished = polish_narrative(
            template_narrative, stock_name, score,
            portfolio_context=portfolio_context,
            news=news,
        )
        return {"narrative": polished}
    except Exception as e:
        logger.warning(f"LLM 润色失败: {e}")
        return {"narrative": template_narrative}


@mcp.tool()
async def get_regime():
    """
    今日潮势 — 大盘现在是顺风出海还是暴风骤雨？

    通过上证指数的均线斜率、波动率、涨跌比等指标，
    识别市场处于 牛市/熊市/横盘/高波动 中的哪个阶段。
    不同体制下，同一技术形态的含义完全不同。

    Returns:
        当前市场体制及其对交易信号的调整建议
    """
    logger.info("🌊 感知潮势...")

    index_df = market_data.get_index_daily("000001", days=120)
    regime_result = regime_detector.detect(index_df)

    if regime_result["regime"] == "unknown":
        return regime_result

    adjustment = regime_detector.get_regime_adjustment(regime_result["regime"])

    # 额外获取几个关键指数的当日表现
    indices = {}
    for code, name in [("000001", "上证指数"), ("399001", "深证成指"), ("399006", "创业板指")]:
        try:
            idx_df = market_data.get_index_daily(code, days=5)
            if not idx_df.empty:
                latest = idx_df["close"].iloc[-1]
                prev = idx_df["close"].iloc[-2]
                indices[name] = {
                    "close": round(latest, 2),
                    "pct_change": round((latest / prev - 1) * 100, 2),
                }
        except Exception:
            pass

    return {
        "regime": regime_result,
        "adjustment": adjustment,
        "indices": indices,
        "timestamp": _now_bj().isoformat(),
    }


@mcp.tool()
async def compare_stocks(symbols: str):
    """
    多股横向对比 — 在多只股票间比较技术面强弱

    输入逗号分隔的股票代码，对比它们的技术评分、
    趋势强度、量能状况等指标，帮助筛选最强标的。
    注意：对比基于原始技术评分（未加体制调整），跨市场对比（如 AAPL vs 600519）可直接比较。

    Args:
        symbols: 逗号分隔的股票代码，如 "002111,600519,000858" 或 "AAPL,MSFT,TSLA"

    Returns:
        横向对比表格，按技术评分排序
    """
    codes = [s.strip() for s in symbols.split(",") if s.strip()]
    if len(codes) < 2:
        return {"error": "至少需要两只股票进行对比，用逗号分隔"}

    results = []
    for code in codes[:10]:  # 最多10只
        name = market_data.get_stock_name(code)
        df = market_data.get_stock_daily(code, days=120)
        if df.empty:
            results.append({"code": code, "name": name, "error": "数据获取失败"})
            continue

        tech = technical.analyze(df)
        if "error" in tech:
            results.append({"code": code, "name": name, "error": tech["error"]})
            continue

        results.append({
            "code": code,
            "name": name,
            "price": tech["price_position"]["price"],
            "score": tech["trend"]["score"],
            "signal": tech["trend"]["signal"],
            "rsi": tech["momentum"]["rsi_14"],
            "macd_cross": tech["momentum"]["macd_cross"],
            "volume_ratio": tech["volume"]["volume_ratio"],
            "pct_5d": tech["price_position"]["pct_5d"],
            "pct_20d": tech["price_position"]["pct_20d"],
            "position_20d": tech["price_position"]["position_20d"],
            "bullish_aligned": tech["ma"]["bullish_aligned"],
            "patterns": tech["patterns"],
        })

    # 按评分排序
    valid = [r for r in results if "score" in r]
    valid.sort(key=lambda x: x["score"], reverse=True)
    errors = [r for r in results if "error" in r]

    return {
        "comparison": valid + errors,
        "strongest": valid[0]["code"] if valid else None,
        "weakest": valid[-1]["code"] if valid else None,
        "timestamp": _now_bj().isoformat(),
    }


@mcp.tool()
async def get_money_flow_detail(symbol: str, days: int = 10):
    """
    资金流向详细分析 — 查看主力、大单、散户的进出

    分析个股的资金流向趋势，判断主力是在建仓还是出货。
    包含最近N日的资金流向历史和趋势判断。

    Args:
        symbol: 股票代码
        days: 查看天数

    Returns:
        资金流向分析报告
    """
    name = market_data.get_stock_name(symbol)

    # 当日快照
    current = market_data.get_money_flow(symbol)

    # 历史趋势
    history_df = market_data.get_money_flow_history(symbol, days=days)

    history_summary = {}
    if not history_df.empty and "main_net" in history_df.columns:
        main_net = history_df["main_net"]
        history_summary = {
            "total_main_net": round(main_net.sum(), 2),
            "avg_main_net": round(main_net.mean(), 2),
            "positive_days": int((main_net > 0).sum()),
            "negative_days": int((main_net < 0).sum()),
            "trend": "持续流入" if main_net.tail(3).mean() > 0 else "持续流出",
        }

    # 龙虎榜
    lhb = market_data.get_lhb(symbol)

    return {
        "stock": {"code": symbol, "name": name},
        "current": current,
        "history": history_summary,
        "lhb_records": lhb,
        "timestamp": _now_bj().isoformat(),
    }


@mcp.tool()
async def get_stock_news_report(symbol: str, limit: int = 10):
    """
    个股新闻消息面 — 获取最新相关新闻

    抓取个股相关新闻，帮助判断消息面是利好还是利空。

    Args:
        symbol: 股票代码
        limit: 新闻条数

    Returns:
        新闻列表
    """
    name = market_data.get_stock_name(symbol)
    news = market_data.get_stock_news(symbol, limit=limit)

    return {
        "stock": {"code": symbol, "name": name},
        "news": news,
        "count": len(news),
        "timestamp": _now_bj().isoformat(),
    }


@mcp.tool()
async def get_north_flow_report(days: int = 20):
    """
    北向资金分析 — 外资动向

    查看近期北向资金（沪股通+深股通）的净流入情况，
    外资被视为"聪明钱"，其动向对大盘有领先指示作用。

    Args:
        days: 查看天数

    Returns:
        北向资金流向报告
    """
    df = market_data.get_north_flow(days=days)
    if df.empty:
        return {"error": "北向资金数据获取失败"}

    net = df["net_inflow"] if "net_inflow" in df.columns else pd.Series()
    summary = {}
    if not net.empty:
        summary = {
            "total_net": round(net.sum(), 2),
            "avg_daily": round(net.mean(), 2),
            "positive_days": int((net > 0).sum()),
            "negative_days": int((net < 0).sum()),
            "recent_3d": round(net.tail(3).sum(), 2),
            "trend": "净流入" if net.tail(5).mean() > 0 else "净流出",
        }

    return {
        "north_flow": summary,
        "days": days,
        "timestamp": _now_bj().isoformat(),
    }


@mcp.tool()
async def server_status():
    """
    查看观潮服务器状态

    Returns:
        服务器运行状态、版本信息、统计数据
    """
    return {
        "name": "观潮 (TideWatch)",
        "version": VERSION,
        "description": "AI 投研搭档 — 多维融合股票分析引擎",
        "stats": server_stats,
        "tools": [t.name for t in await mcp.list_tools()],
    }


@mcp.tool()
async def review_signals(days: int = 30, symbol: str = "", limit: int = 200):
    """
    查看历史信号和胜率统计 — 观潮的自省系统

    回顾过去 N 天的所有分析信号，检查哪些判断对了、哪些错了。
    胜率数据需要先运行 update_signal_outcomes 回填。

    Args:
        days: 查看天数（默认30天）
        symbol: 可选，指定股票代码只看该票
        limit: 最多返回信号条数（默认200）

    Returns:
        信号列表 + 胜率统计
    """
    stats = get_signal_stats(days=days)
    recent = get_recent_signals(days=days, symbol=symbol if symbol else None)

    # 简化信号列表（只保留关键字段）
    signals_summary = []
    for s in recent[:limit]:
        entry = {
            "id": s["id"],
            "date": s["timestamp"][:10],
            "time": s["timestamp"][11:16],  # HH:MM
            "symbol": s["symbol"],
            "name": s["name"],
            "direction": s["direction"],
            "score": s["score"],
            "price": s["price_at_signal"],
        }
        # 添加回填结果（如果有）
        if s.get("pct_5d") is not None:
            entry["5d"] = f"{s['pct_5d']:+.1f}% ({s['outcome_5d']})"
        if s.get("pct_10d") is not None:
            entry["10d"] = f"{s['pct_10d']:+.1f}% ({s['outcome_10d']})"
        if s.get("pct_20d") is not None:
            entry["20d"] = f"{s['pct_20d']:+.1f}% ({s['outcome_20d']})"
        signals_summary.append(entry)

    return {
        "stats": stats,
        "signals": signals_summary,
        "timestamp": _now_bj().isoformat(),
    }


@mcp.tool()
async def update_signal_outcomes():
    """
    回填历史信号的实际走势 — 计算胜率

    检查所有未回填的历史信号，获取信号发出后 5/10/20 个交易日的实际价格，
    计算涨跌幅并判断信号是否正确。

    建议每天收盘后运行一次。

    Returns:
        回填统计（更新了多少条 5d/10d/20d 记录）
    """
    result = await asyncio.to_thread(update_outcomes, market_data)
    return {
        "updated": result,
        "message": f"回填完成: 5日={result['5d']}条, 10日={result['10d']}条, 20日={result['20d']}条",
        "timestamp": _now_bj().isoformat(),
    }


@mcp.tool()
async def manage_holdings(
    action: str,
    symbol: str = "",
    cost: float = 0,
    shares: int = 0,
):
    """
    持仓管理 — 添加、移除、查看持仓

    Args:
        action: 操作类型 ("add" / "remove" / "list")
        symbol: 股票代码（add/remove 时必填）
        cost: 买入成本价（add 时填写）
        shares: 持仓数量（add 时填写）

    Returns:
        操作结果和当前持仓列表
    """
    if action == "list":
        holdings = get_holdings()
        return {
            "holdings": holdings,
            "count": len(holdings),
            "message": f"当前持仓 {len(holdings)} 只" if holdings else "暂无持仓",
        }
    elif action == "add":
        if not symbol:
            return {"error": "请提供股票代码"}
        name = HOT_NAMES.get(symbol) or market_data.get_stock_name(symbol)
        add_holding(symbol, name=name, cost=cost, shares=shares)
        return {
            "message": f"✅ 已添加持仓: {symbol} {name}" + (f" 成本{cost} ×{shares}股" if cost else ""),
            "holdings": get_holdings(),
        }
    elif action == "remove":
        if not symbol:
            return {"error": "请提供股票代码"}
        remove_holding(symbol)
        return {
            "message": f"✅ 已移除持仓: {symbol}",
            "holdings": get_holdings(),
        }
    else:
        return {"error": f"未知操作: {action}，请用 add/remove/list"}


@mcp.tool()
async def manage_watchlist(
    action: str,
    symbol: str = "",
    reason: str = "",
):
    """
    自选股管理 — 添加、移除、查看自选

    Args:
        action: 操作类型 ("add" / "remove" / "list")
        symbol: 股票代码（add/remove 时必填）
        reason: 关注原因（add 时可选，如 "底部放量" "等回调"）

    Returns:
        操作结果和当前自选列表
    """
    if action == "list":
        watchlist = get_watchlist()
        return {
            "watchlist": watchlist,
            "count": len(watchlist),
            "message": f"当前自选 {len(watchlist)} 只" if watchlist else "暂无自选股",
        }
    elif action == "add":
        if not symbol:
            return {"error": "请提供股票代码"}
        name = HOT_NAMES.get(symbol) or market_data.get_stock_name(symbol)
        add_watchlist(symbol, name=name, reason=reason)
        return {
            "message": f"✅ 已添加自选: {symbol} {name}" + (f" ({reason})" if reason else ""),
            "watchlist": get_watchlist(),
        }
    elif action == "remove":
        if not symbol:
            return {"error": "请提供股票代码"}
        remove_watchlist(symbol)
        return {
            "message": f"✅ 已移除自选: {symbol}",
            "watchlist": get_watchlist(),
        }
    else:
        return {"error": f"未知操作: {action}，请用 add/remove/list"}


@mcp.tool()
async def manage_account(
    action: str,
    cash: float = 0,
    total_assets: float = 0,
    market_value: float = 0,
):
    """
    账户资金管理 — 更新或查看账户资金信息

    用于记录真实账户的可用资金、总资产和持仓市值，以便 AI 分析时了解仓位空间。
    注意：当前账户资金以人民币计价，美股持仓需自行折算。

    Args:
        action: 操作类型 ("update" / "view")
        cash: 可用资金（update 时填写）
        total_assets: 总资产（update 时填写，0 则不更新）
        market_value: 持仓市值（update 时填写，0 则不更新）

    Returns:
        账户资金摘要 + 持仓概览
    """
    if action == "view":
        account = get_account_info()
        holdings = get_holdings()
        return {
            "account": account,
            "holdings_count": len(holdings),
            "message": f"可用资金 ¥{account['cash']:,.2f} | 总资产 ¥{account['total_assets']:,.2f}" if account["cash"] else "暂未设置账户信息",
        }
    elif action == "update":
        if cash <= 0 and total_assets <= 0:
            return {"error": "请提供 cash（可用资金）或 total_assets（总资产）"}
        set_account_info(cash=cash, total_assets=total_assets, market_value=market_value)
        account = get_account_info()
        return {
            "message": f"✅ 账户已更新: 可用 ¥{account['cash']:,.2f} | 总资产 ¥{account['total_assets']:,.2f} | 市值 ¥{account['market_value']:,.2f}",
            "account": account,
        }
    else:
        return {"error": f"未知操作: {action}，请用 update/view"}


@mcp.tool()
async def scan_market(top_n: int = 10):
    """
    三级股票池扫描 — 持仓 + 自选 + 热门，按技术评分排序

    从三级股票池（持仓 > 自选 > 热门80只）批量拉K线做技术分析评分，
    持仓显示浮盈浮亏，所有股票按评分排序。

    不依赖全市场实时行情接口（push2 反爬），使用日K线接口。

    Args:
        top_n: 热门池中返回最强/最弱各多少只（默认10）

    Returns:
        持仓全部 + 自选全部 + 热门 Top/Bottom N，按评分排序
    """
    global _scan_bg_refreshing
    logger.info(f"🔍 三级股票池扫描: Top/Bottom {top_n}")
    server_stats["scans_completed"] += 1

    cache_age = _time.monotonic() - _scan_cache["time"] if _scan_cache["result"] else float("inf")

    # 1) 新鲜缓存（5分钟内）→ 直接返回
    if _scan_cache["result"] and cache_age < _SCAN_CACHE_TTL:
        logger.info("⚙️ 使用扫描缓存（%ds内）", int(_SCAN_CACHE_TTL - cache_age))
        return _slice_scan_cache(top_n)

    # 2) Stale-While-Revalidate: 有旧缓存 → 先返回旧数据，后台异步刷新
    #    - 非盘中 + 缓存是收盘后数据：数据不变，直接返回
    #    - 非盘中 + 缓存是盘中数据：强制刷新（修复 2026-04-08 盲区：13:01 缓存冻结到次日）
    #    - 盘中：返回旧缓存 + 后台刷新，下次请求拿到新数据
    if _scan_cache["result"]:
        if not _is_market_hours():
            # 检查缓存是否是盘中的旧快照（收盘前数据，需刷新为收盘价）
            should_force = False
            cache_ts_str = _scan_cache["result"].get("timestamp", "")
            if cache_ts_str:
                try:
                    cache_ts = datetime.fromisoformat(cache_ts_str)
                    if not cache_ts.tzinfo:
                        cache_ts = cache_ts.replace(tzinfo=_BJ_TZ)
                    now = _now_bj()
                    # 今天 15:30 前的缓存可能不含完整收盘数据（baostock 结算延迟）
                    if (cache_ts.date() == now.date()
                            and cache_ts.hour * 60 + cache_ts.minute < 15 * 60 + 30
                            and now.weekday() < 5):
                        should_force = True
                        logger.info("⚙️ 非盘中但缓存是盘中数据（%s），强制刷新...", cache_ts.strftime("%H:%M"))
                except Exception as e:
                    logger.debug("缓存 timestamp 解析失败: %s", e)
            if not should_force:
                logger.info("⚙️ 非盘中，使用已有缓存（数据不变）")
                return _slice_scan_cache(top_n)
            # 强制刷新：走 _run_scan_warmup 确保持久化到 scan_cache.json
            await asyncio.to_thread(_run_scan_warmup)
            return _slice_scan_cache(top_n)
        # 盘中过期，先返回后台刷新
        if not _scan_bg_refreshing:
            _scan_bg_refreshing = True
            logger.info("♻️ 返回过期缓存（%ds前），后台刷新中...", int(cache_age))
            asyncio.get_event_loop().run_in_executor(None, _bg_refresh_scan)
        else:
            logger.info("♻️ 返回过期缓存（%ds前），后台刷新已在进行", int(cache_age))
        return _slice_scan_cache(top_n)

    # 3) 完全无缓存 → 阻塞扫描（首次冷启动）
    logger.info("🔍 无缓存，执行全量扫描...")
    await asyncio.to_thread(_run_scan_warmup)
    return _slice_scan_cache(top_n)


def _slice_scan_cache(top_n: int):
    """从缓存中按 top_n 切片返回（过滤内部字段）"""
    cached = _scan_cache["result"]
    hot_all = cached.get("_hot_sorted", [])
    output = {k: v for k, v in cached.items() if not k.startswith("_")}
    output["hot_strongest"] = hot_all[:top_n]
    output["hot_weakest"] = sorted(hot_all[-top_n:], key=lambda x: x["score"]) if len(hot_all) > top_n else []
    return output


def _bg_refresh_scan():
    """后台刷新 scan_market 缓存（在线程池中执行）"""
    global _scan_bg_refreshing
    try:
        _run_scan_warmup()
        logger.info("♻️ 后台刷新完成，缓存已更新")
    except Exception as e:
        logger.error(f"♻️ 后台刷新失败: {e}")
    finally:
        _scan_bg_refreshing = False



# ============================================================================
# 内部辅助函数
# ============================================================================


def _calc_confidence(adjusted_score: int, symbol: str) -> int:
    """
    v3 置信度计算 — 过度自信衰减 + 翻转惩罚

    数据依据 (91条回填):
    - |score| [70,85): 89.5% 胜率（甜蜜区）
    - |score| [85,95): 56.3% 胜率（过度自信区）
    - 翻转信号: 30% 胜率 vs 非翻转 71.6%
    """
    conf = min(abs(adjusted_score), 100)

    # 过度自信衰减：|score|>=85 时 confidence × 0.7
    if abs(adjusted_score) >= 85:
        conf = int(conf * 0.7)

    # 翻转惩罚：方向和上一条信号相反时 × 0.6
    try:
        recent = get_recent_signals(days=7, symbol=symbol)
        if recent:
            prev = recent[0]
            prev_dir = prev.get("direction", "")
            curr_bullish = adjusted_score >= 8
            curr_bearish = adjusted_score <= -8
            prev_bullish = prev_dir in ("看多", "偏多")
            prev_bearish = prev_dir in ("看空", "偏空")
            if (curr_bullish and prev_bearish) or (curr_bearish and prev_bullish):
                conf = int(conf * 0.6)
    except Exception:
        pass  # 翻转检测失败不阻塞

    return max(conf, 1)


def _detect_conflicts(
    tech: dict, money: dict, regime: dict
) -> list[dict[str, str]]:
    """
    冲突检测 — 发现技术面与其它维度的矛盾信号

    这是观潮的核心差异化功能：
    "技术面看多但主力在出货" 这种矛盾才是真正有价值的洞察
    """
    conflicts = []
    trend = tech.get("trend", {})
    score = trend.get("score", 0)

    # 冲突1: 技术面看多但资金在流出
    if score > 10 and money.get("main_net_inflow", 0) < 0:
        conflicts.append({
            "type": "tech_vs_money",
            "severity": "high",
            "description": "⚠️ 技术面偏多但主力资金在流出，可能是诱多",
            "advice": "等资金回流确认后再进场",
        })

    # 冲突2: 技术面看空但资金在流入
    if score < -10 and money.get("main_net_inflow", 0) > 0:
        conflicts.append({
            "type": "tech_vs_money",
            "severity": "medium",
            "description": "📋 技术面偏空但主力在吸筹，可能是洗盘",
            "advice": "关注是否止跌企稳形成底部",
        })

    # 冲突3: 个股强但大盘弱
    regime_name = regime.get("regime", "")
    if score > 20 and regime_name in ("bear", "mild_bear"):
        conflicts.append({
            "type": "stock_vs_market",
            "severity": "medium",
            "description": "📉 个股技术面强但大盘偏弱，逆势走强需关注持续性",
            "advice": "轻仓试探，设好止损，大盘转好再加仓",
        })

    # 冲突4: 放量下跌
    vol = tech.get("volume", {})
    if vol.get("expanding") and tech.get("price_position", {}).get("pct_5d", 0) < -3:
        conflicts.append({
            "type": "volume_price",
            "severity": "high",
            "description": "🚨 放量下跌，可能是主力出逃信号",
            "advice": "避免抄底，等缩量企稳",
        })

    # 冲突5: 缩量上涨
    if vol.get("shrinking") and tech.get("price_position", {}).get("pct_5d", 0) > 3:
        conflicts.append({
            "type": "volume_price",
            "severity": "medium",
            "description": "📊 缩量上涨，上行动能不足",
            "advice": "关注后续量能能否跟上，否则可能是假突破",
        })

    return conflicts


# ============================================================================
# HTTP Transport: API Key Authentication Middleware
# ============================================================================


class APIKeyMiddleware:
    """Pure ASGI middleware to validate API key for protected endpoints."""

    PUBLIC_PATHS = {"/health", "/favicon.ico"}

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        if not MCP_API_KEY_ENABLED:
            await self.app(scope, receive, send)
            return

        path = scope["path"]
        if path in self.PUBLIC_PATHS:
            await self.app(scope, receive, send)
            return

        # Extract API key from headers
        api_key = None
        headers = dict(scope.get("headers", []))
        api_key = headers.get(b"x-api-key", b"").decode()
        if not api_key:
            auth_header = headers.get(b"authorization", b"").decode()
            if auth_header.startswith("Bearer "):
                api_key = auth_header[7:]

        if api_key != MCP_API_KEY:
            from starlette.responses import JSONResponse
            response = JSONResponse(
                {"error": "Unauthorized", "message": "Invalid or missing API key"},
                status_code=401,
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)


def _build_middleware():
    """Build middleware list for HTTP mode."""
    from starlette.middleware import Middleware
    from starlette.middleware.cors import CORSMiddleware

    middlewares = [
        Middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]),
    ]
    if MCP_API_KEY_ENABLED:
        middlewares.append(Middleware(APIKeyMiddleware))
    return middlewares


def _create_http_app():
    """Create ASGI app for uvicorn direct usage."""
    return mcp.http_app(
        path="/mcp",
        middleware=_build_middleware(),
        json_response=True,
        stateless_http=True,
    )


# Expose for `uvicorn tidewatch.server:http_app`
http_app = _create_http_app()


# ============================================================================
# Entry Point
# ============================================================================


def main():
    """启动 TideWatch MCP Server（支持 stdio / http 双模式）"""
    parser = argparse.ArgumentParser(description="观潮 (TideWatch) MCP Server")
    parser.add_argument("--http", action="store_true", help="Run in HTTP mode (default: stdio)")
    parser.add_argument("--host", default="0.0.0.0", help="HTTP host (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8889, help="HTTP port (default: 8889)")
    args = parser.parse_args()

    logger.info("🌊 观潮 (TideWatch) MCP Server 启动中...")
    logger.info(f"版本: {VERSION}")
    logger.info("工具: analyze_stock, get_regime, compare_stocks, get_money_flow_detail, get_stock_news_report, get_north_flow_report")

    if args.http:
        logger.info(f"传输模式: HTTP (Streamable HTTP) → {args.host}:{args.port}/mcp")
        logger.info(f"API Key 认证: {'✅ 已启用' if MCP_API_KEY_ENABLED else '❌ 未配置'}")
        mcp.run(
            transport="streamable-http",
            host=args.host,
            port=args.port,
            path="/mcp",
            middleware=_build_middleware(),
            json_response=True,
            stateless_http=True,
        )
    else:
        logger.info("传输模式: stdio (本地)")
        mcp.run()


if __name__ == "__main__":
    main()
