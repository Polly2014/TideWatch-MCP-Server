"""
投资组合管理 — Portfolio Manager
三级股票池：持仓 > 自选 > 热门
持仓和自选存 SQLite，热门硬编码
"""

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# 持仓/账户变更时自动 invalidate scan_cache 的回调
# server.py 启动时注册，portfolio.py 在每次写 DB 后调用
_on_portfolio_changed: list = []

def register_change_callback(fn):
    """注册持仓/账户变更回调（server.py 调用一次）"""
    _on_portfolio_changed.append(fn)

def _notify_change():
    """通知所有注册方：持仓或账户数据已变更"""
    for fn in _on_portfolio_changed:
        try:
            fn()
        except Exception as e:
            logger.debug(f"change callback failed: {e}")

# 北京时间 UTC+8（Azure VM 默认 UTC）
_BJ_TZ = timezone(timedelta(hours=8))

def _now_bj():
    return datetime.now(_BJ_TZ)

DB_PATH = Path(__file__).parent.parent.parent / "data" / "signals.db"

# ============================================================================
# 热门股票池 — 8 赛道 ~70 只核心标的 (代码, 名称)
# ============================================================================

HOT_POOL: dict[str, list[tuple[str, str]]] = {
    "大盘权重": [
        ("601318", "中国平安"),
        ("600036", "招商银行"),
        ("600900", "长江电力"),
    ],
    "新能源": [
        ("300750", "宁德时代"),
        ("002594", "比亚迪"),
        ("601012", "隆基绿能"),
    ],
    "半导体AI": [
        ("688981", "中芯国际"),
        ("688256", "寒武纪"),
        ("002371", "北方华创"),
    ],
    "消费": [
        ("600519", "贵州茅台"),
        ("000858", "五粮液"),
        ("600809", "山西汾酒"),
    ],
    "医药": [
        ("600276", "恒瑞医药"),
        ("300760", "迈瑞医疗"),
        ("603259", "药明康德"),
    ],
    "军工": [
        ("600760", "中航沈飞"),
        ("600150", "中国船舶"),
        ("002414", "高德红外"),
    ],
    "资源": [
        ("601899", "紫金矿业"),
        ("600547", "山东黄金"),
        ("601088", "中国神华"),
    ],
    "地产基建": [
        ("600048", "保利发展"),
        ("600585", "海螺水泥"),
        ("601668", "中国建筑"),
    ],
}

# 代码 → 名称 快速查找表
HOT_NAMES: dict[str, str] = {code: name for stocks in HOT_POOL.values() for code, name in stocks}


def _get_hot_symbols() -> set[str]:
    """获取热门股票池所有代码"""
    return set(HOT_NAMES.keys())


# ============================================================================
# SQLite 持仓/自选管理
# ============================================================================

def _get_conn() -> sqlite3.Connection:
    """获取数据库连接（自动建表）"""
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS holdings (
            symbol TEXT PRIMARY KEY,
            name TEXT,
            cost REAL,
            shares INTEGER DEFAULT 0,
            added_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS watchlist (
            symbol TEXT PRIMARY KEY,
            name TEXT,
            reason TEXT,
            added_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS account_info (
            key TEXT PRIMARY KEY,
            value REAL NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn


# --- 持仓 ---

def add_holding(symbol: str, name: str = "", cost: float = 0, shares: int = 0):
    """添加或更新持仓

    防御：当传入 name 为空或等于 symbol（解析失败的常见结果），
    且 DB 中已存在有效 name，则保留旧值，避免覆盖污染。
    """
    conn = _get_conn()
    if not name or name == symbol:
        row = conn.execute("SELECT name FROM holdings WHERE symbol = ?", (symbol,)).fetchone()
        if row and row["name"] and row["name"] != symbol:
            name = row["name"]
            logger.info(f"📌 保留已有 name: {symbol} → {name}")
    conn.execute(
        "INSERT OR REPLACE INTO holdings (symbol, name, cost, shares, added_at) VALUES (?, ?, ?, ?, ?)",
        (symbol, name, cost, shares, _now_bj().isoformat()),
    )
    conn.commit()
    conn.close()
    logger.info(f"📌 持仓更新: {symbol} {name} cost={cost} shares={shares}")
    _notify_change()


def remove_holding(symbol: str):
    """移除持仓"""
    conn = _get_conn()
    conn.execute("DELETE FROM holdings WHERE symbol = ?", (symbol,))
    conn.commit()
    conn.close()
    logger.info(f"📌 持仓移除: {symbol}")
    _notify_change()


def get_holdings() -> list[dict]:
    """获取所有持仓"""
    conn = _get_conn()
    rows = conn.execute("SELECT * FROM holdings ORDER BY added_at").fetchall()
    conn.close()
    return [dict(r) for r in rows]


# --- 账户资金 ---

def set_account_info(cash: float, total_assets: float = 0, market_value: float = 0):
    """更新账户资金信息"""
    conn = _get_conn()
    now = _now_bj().isoformat()
    if cash > 0:
        conn.execute(
            "INSERT OR REPLACE INTO account_info (key, value, updated_at) VALUES (?, ?, ?)",
            ("cash", cash, now),
        )
    if total_assets > 0:
        conn.execute(
            "INSERT OR REPLACE INTO account_info (key, value, updated_at) VALUES (?, ?, ?)",
            ("total_assets", total_assets, now),
        )
    if market_value > 0:
        conn.execute(
            "INSERT OR REPLACE INTO account_info (key, value, updated_at) VALUES (?, ?, ?)",
            ("market_value", market_value, now),
        )
    conn.commit()
    conn.close()
    logger.info(f"💰 账户更新: 可用={cash}, 总资产={total_assets}, 市值={market_value}")
    _notify_change()


def get_account_info() -> dict:
    """获取账户资金信息"""
    conn = _get_conn()
    rows = conn.execute("SELECT key, value, updated_at FROM account_info").fetchall()
    conn.close()
    info = {r["key"]: {"value": r["value"], "updated_at": r["updated_at"]} for r in rows}
    return {
        "cash": info.get("cash", {}).get("value", 0),
        "total_assets": info.get("total_assets", {}).get("value", 0),
        "market_value": info.get("market_value", {}).get("value", 0),
        "updated_at": info.get("cash", {}).get("updated_at", ""),
    }


# --- 自选 ---

def add_watchlist(symbol: str, name: str = "", reason: str = ""):
    """添加自选股

    防御：当传入 name 为空或等于 symbol（解析失败的常见结果），
    且 DB 中已存在有效 name，则保留旧值，避免覆盖污染。
    """
    conn = _get_conn()
    if not name or name == symbol:
        row = conn.execute("SELECT name FROM watchlist WHERE symbol = ?", (symbol,)).fetchone()
        if row and row["name"] and row["name"] != symbol:
            name = row["name"]
            logger.info(f"👀 保留已有 name: {symbol} → {name}")
    conn.execute(
        "INSERT OR REPLACE INTO watchlist (symbol, name, reason, added_at) VALUES (?, ?, ?, ?)",
        (symbol, name, reason, _now_bj().isoformat()),
    )
    conn.commit()
    conn.close()
    logger.info(f"👀 自选添加: {symbol} {name}")
    _notify_change()


def remove_watchlist(symbol: str):
    """移除自选股"""
    conn = _get_conn()
    conn.execute("DELETE FROM watchlist WHERE symbol = ?", (symbol,))
    conn.commit()
    conn.close()
    logger.info(f"👀 自选移除: {symbol}")
    _notify_change()


def get_watchlist() -> list[dict]:
    """获取所有自选股"""
    conn = _get_conn()
    rows = conn.execute("SELECT * FROM watchlist ORDER BY added_at").fetchall()
    conn.close()
    return [dict(r) for r in rows]


# --- 合并池 ---

def get_scan_pool() -> dict[str, list[str]]:
    """获取三级扫描池（去重）

    Returns:
        {"holdings": [...], "watchlist": [...], "hot": [...]}
        hot 中已排除 holdings 和 watchlist 中的重复代码
    """
    holdings = get_holdings()
    watchlist = get_watchlist()

    holding_symbols = [h["symbol"] for h in holdings]
    watchlist_symbols = [w["symbol"] for w in watchlist]

    seen = set(holding_symbols) | set(watchlist_symbols)
    hot_symbols = [s for s in sorted(_get_hot_symbols()) if s not in seen]

    return {
        "holdings": holding_symbols,
        "watchlist": watchlist_symbols,
        "hot": hot_symbols,
    }
