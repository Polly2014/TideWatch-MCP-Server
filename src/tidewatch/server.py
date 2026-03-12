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

import argparse
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
from dotenv import load_dotenv
from fastmcp import FastMCP

from .data import MarketData
from .guardrails import check_guardrails
from .llm import polish_narrative
from .narrative import NarrativeGenerator
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
# 也加载 .env（如果存在，用于雪球 token 等额外配置）
env_path = PROJECT_ROOT / ".env"
if env_path.exists():
    load_dotenv(env_path, override=False)

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

VERSION = "0.2.0"

# 服务器统计
server_stats = {
    "start_time": datetime.now().isoformat(),
    "analyses_completed": 0,
    "scans_completed": 0,
}


# ============================================================================
# MCP Tools
# ============================================================================


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request):
    """Health check endpoint (no auth required)"""
    from starlette.responses import JSONResponse
    return JSONResponse({
        "status": "healthy",
        "server": "TideWatch-观潮",
        "version": VERSION,
        "transport": "streamable-http",
        "tools_count": 10,
        "auth_enabled": MCP_API_KEY_ENABLED,
        "analyses_completed": server_stats["analyses_completed"],
    })


@mcp.tool()
async def analyze_stock(
    symbol: str,
    include_news: bool = True,
    include_money_flow: bool = True,
    days: int = 120,
):
    """
    个股综合分析 — 技术面 + 资金面 + 消息面 + 市场体制

    这是观潮的核心分析工具，对一只股票进行多维度交叉分析。
    不只看技术指标，还会结合当前大盘体制、资金流向、新闻消息，
    输出带有冲突检测的综合研判。

    Args:
        symbol: 股票代码（纯数字，如 "002111"）
        include_news: 是否包含新闻消息面分析
        include_money_flow: 是否包含资金流向
        days: K线天数（默认120个交易日）

    Returns:
        综合分析报告，包含技术面、资金面、消息面、市场体制、冲突检测
    """
    logger.info(f"📊 开始分析: {symbol}")

    # 1. 获取日K线（核心数据源，必须成功）
    df = market_data.get_stock_daily(symbol, days=days)
    if df.empty:
        return {"error": f"无法获取 {symbol} 的行情数据"}

    # 2. 技术分析（基于日K线，不依赖实时数据）
    tech = technical.analyze(df)
    if "error" in tech:
        return tech

    # 股票名称和实时行情（可选，失败时 fallback 到日K线）
    try:
        stock_name = market_data.get_stock_name(symbol)
    except Exception:
        stock_name = symbol
    if stock_name == symbol:
        # 日K线列名里没有名称，保持代码
        stock_name = symbol
    try:
        realtime = market_data.get_stock_realtime(symbol)
    except Exception:
        realtime = {}
    # fallback: 实时数据不可用时用日K线最后一条补全
    if realtime.get("fallback") or not realtime.get("price"):
        last = df.iloc[-1]
        realtime = {
            "price": float(last["close"]),
            "pct_change": float(last.get("pct_change", 0)),
            "pe": 0,
            "pb": 0,
            "total_mv": 0,
        }

    # 3. 市场体制
    index_code = "000001"  # 上证指数
    index_df = market_data.get_index_daily(index_code, days=days)
    regime_result = regime_detector.detect(index_df)
    regime_adj = regime_detector.get_regime_adjustment(regime_result["regime"])

    # ETF 没有资金流向和个股新闻
    is_etf = market_data._is_etf(symbol)

    # 4. 资金面
    money = {}
    if include_money_flow and not is_etf:
        money = market_data.get_money_flow(symbol)

    # 5. 消息面
    news = []
    if include_news and not is_etf:
        news = market_data.get_stock_news(symbol, limit=5)

    # 6. 冲突检测
    conflicts = _detect_conflicts(tech, money, regime_result)

    # 7. 综合评分（技术面得分 + 体制调整）
    raw_score = tech["trend"]["score"]
    adjusted_score = raw_score + regime_adj["signal_bias"]
    adjusted_score = max(-100, min(100, adjusted_score))

    # 8. 最终信号（与 technical.py 阈值同步）
    if adjusted_score >= 25:
        final_signal = "看多"
    elif adjusted_score >= 8:
        final_signal = "偏多"
    elif adjusted_score <= -25:
        final_signal = "看空"
    elif adjusted_score <= -8:
        final_signal = "偏空"
    else:
        final_signal = "中性观望"

    server_stats["analyses_completed"] += 1

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
            "confidence": min(abs(adjusted_score), 100),
            "regime_adjustment": regime_adj["signal_bias"],
        },
        "technical": tech,
        "regime": regime_result,
        "money_flow": money,
        "news": news,
        "conflicts": conflicts,
        "advice": {
            "position_max": f"{regime_adj['position_max'] * 100:.0f}%",
            "stop_loss_hint": f"ATR止损建议: 价格 - {tech['volatility']['atr_14'] * regime_adj['stop_loss_multiplier']:.2f}",
        },
        "narrative": narrator.generate(stock_name, tech, regime_result, money, conflicts, final_signal),
        "timestamp": datetime.now().isoformat(),
    }

    # 9. LLM 叙事润色（可选，失败时保留模板叙事）
    try:
        report["narrative"] = polish_narrative(
            report["narrative"], stock_name, adjusted_score
        )
    except Exception as e:
        logger.debug(f"LLM 润色跳过: {e}")

    # 10. 行为护栏检测
    try:
        guardrail_warnings = check_guardrails(symbol, tech)
        if guardrail_warnings:
            report["guardrails"] = guardrail_warnings
            # 护栏警告也加入叙事末尾
            guardrail_text = "\n\n".join(
                f"{w['message']} {w['advice']}" for w in guardrail_warnings
            )
            report["narrative"] += "\n\n" + guardrail_text
    except Exception as e:
        logger.warning(f"行为护栏检测失败: {e}")

    # 10. 记录信号到追踪系统（自动，不影响主流程）
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

    return report


@mcp.tool()
async def get_regime():
    """
    市场体制识别 — 判断当前大盘处于什么阶段

    通过上证指数的均线斜率、波动率、涨跌比等指标，
    识别市场处于 牛市/熊市/横盘/高波动 中的哪个阶段。
    不同体制下，同一技术形态的含义完全不同。

    Returns:
        当前市场体制及其对交易信号的调整建议
    """
    logger.info("🌊 分析市场体制")

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
        "timestamp": datetime.now().isoformat(),
    }


@mcp.tool()
async def compare_stocks(symbols: str):
    """
    多股横向对比 — 在多只股票间比较技术面强弱

    输入逗号分隔的股票代码，对比它们的技术评分、
    趋势强度、量能状况等指标，帮助筛选最强标的。

    Args:
        symbols: 逗号分隔的股票代码，如 "002111,600519,000858"

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
        "timestamp": datetime.now().isoformat(),
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
        "timestamp": datetime.now().isoformat(),
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
        "timestamp": datetime.now().isoformat(),
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
        "timestamp": datetime.now().isoformat(),
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
        "version": "0.2.0",
        "description": "AI 投研搭档 — 多维融合股票分析引擎",
        "stats": server_stats,
        "tools": [
            "analyze_stock — 个股综合分析（核心工具）",
            "get_regime — 市场体制识别",
            "compare_stocks — 多股横向对比",
            "get_money_flow_detail — 资金流向详细分析",
            "get_stock_news_report — 个股新闻消息面",
            "get_north_flow_report — 北向资金分析",
            "review_signals — 查看历史信号和胜率统计",
            "update_signal_outcomes — 回填历史信号实际走势",
            "scan_market — 🆕 全市场扫描强弱股 Top/Bottom N",
        ],
    }


@mcp.tool()
async def review_signals(days: int = 30, symbol: str = ""):
    """
    查看历史信号和胜率统计 — 观潮的自省系统

    回顾过去 N 天的所有分析信号，检查哪些判断对了、哪些错了。
    胜率数据需要先运行 update_signal_outcomes 回填。

    Args:
        days: 查看天数（默认30天）
        symbol: 可选，指定股票代码只看该票

    Returns:
        信号列表 + 胜率统计
    """
    stats = get_signal_stats(days=days)
    recent = get_recent_signals(days=days, symbol=symbol if symbol else None)

    # 简化信号列表（只保留关键字段）
    signals_summary = []
    for s in recent[:50]:  # 最多50条
        entry = {
            "id": s["id"],
            "date": s["timestamp"][:10],
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
        "timestamp": datetime.now().isoformat(),
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
    result = update_outcomes(market_data)
    return {
        "updated": result,
        "message": f"回填完成: 5日={result['5d']}条, 10日={result['10d']}条, 20日={result['20d']}条",
        "timestamp": datetime.now().isoformat(),
    }


@mcp.tool()
async def scan_market(top_n: int = 10):
    """
    全市场扫描 — 找出今日最强和最弱的股票

    从全市场 A 股中，按涨跌幅和量比初筛，然后对候选股做技术分析评分，
    输出技术面最强的 Top N 和最弱的 Bottom N。

    适合用来发现今天的热点和异动，找到值得深入分析的标的。

    Args:
        top_n: 返回最强/最弱各多少只（默认10）

    Returns:
        强势股 Top N + 弱势股 Bottom N，按技术评分排序
    """
    logger.info(f"🔍 全市场扫描: Top/Bottom {top_n}")
    server_stats["scans_completed"] += 1

    # 1. 获取全市场实时数据
    df = market_data._get_spot_cache()
    if df.empty or "代码" not in df.columns:
        return {"error": "全市场数据获取失败，请稍后重试"}

    # 过滤：排除 ST、停牌、新股（上市不足20天用涨跌幅判断）
    filtered = df.copy()
    if "名称" in filtered.columns:
        filtered = filtered[~filtered["名称"].str.contains("ST|退", na=False)]
    if "最新价" in filtered.columns:
        filtered = filtered[filtered["最新价"] > 0]  # 排除停牌
    if "涨跌幅" in filtered.columns:
        filtered = filtered[filtered["涨跌幅"].abs() < 20]  # 排除涨跌停（可能数据异常）

    if filtered.empty:
        return {"error": "过滤后无有效数据"}

    # 2. 初筛：涨幅 Top 30 + 跌幅 Bottom 30
    by_pct = filtered.sort_values("涨跌幅", ascending=False)
    candidates_bull = by_pct.head(30)
    candidates_bear = by_pct.tail(30)

    # 3. 对候选股跑技术分析评分
    def _score_stock(code):
        try:
            daily = market_data.get_stock_daily(str(code), days=60)
            if daily.empty or len(daily) < 20:
                return None
            tech_result = technical.analyze(daily)
            if "error" in tech_result:
                return None
            return {
                "code": str(code),
                "name": str(filtered[filtered["代码"] == code]["名称"].iloc[0]) if not filtered[filtered["代码"] == code].empty else str(code),
                "price": float(by_pct[by_pct["代码"] == code]["最新价"].iloc[0]) if not by_pct[by_pct["代码"] == code].empty else 0,
                "pct_today": float(by_pct[by_pct["代码"] == code]["涨跌幅"].iloc[0]) if not by_pct[by_pct["代码"] == code].empty else 0,
                "score": tech_result["trend"]["score"],
                "signal": tech_result["trend"]["signal"],
                "volume_ratio": tech_result["volume"]["volume_ratio"],
                "rsi": tech_result["momentum"]["rsi_14"],
                "patterns": tech_result["patterns"],
                "reasons_bull": tech_result["trend"]["reasons_bull"][:3],
                "reasons_bear": tech_result["trend"]["reasons_bear"][:3],
            }
        except Exception as e:
            logger.debug(f"扫描 {code} 失败: {e}")
            return None

    # 强势候选
    bull_results = []
    for code in candidates_bull["代码"].values:
        result = _score_stock(code)
        if result:
            bull_results.append(result)
        if len(bull_results) >= top_n * 2:  # 多跑一些以备排序
            break

    # 弱势候选
    bear_results = []
    for code in candidates_bear["代码"].values:
        result = _score_stock(code)
        if result:
            bear_results.append(result)
        if len(bear_results) >= top_n * 2:
            break

    # 4. 按评分排序
    bull_results.sort(key=lambda x: x["score"], reverse=True)
    bear_results.sort(key=lambda x: x["score"])

    return {
        "strongest": bull_results[:top_n],
        "weakest": bear_results[:top_n],
        "total_scanned": len(filtered),
        "timestamp": datetime.now().isoformat(),
    }


# ============================================================================
# 内部辅助函数
# ============================================================================


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
