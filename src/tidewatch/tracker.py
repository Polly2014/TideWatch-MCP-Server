"""
信号追踪系统 — Signal Tracker
每次分析自动记录信号，追踪后续走势，计算历史胜率
这是观潮的"自省系统"——越用越准，时间的朋友
"""

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# 北京时间 UTC+8（Azure VM 默认 UTC，必须显式指定时区）
_BJ_TZ = timezone(timedelta(hours=8))

def _now_bj():
    return datetime.now(_BJ_TZ)

# 数据库路径
DB_PATH = Path(__file__).parent.parent.parent / "data" / "signals.db"


def _get_conn() -> sqlite3.Connection:
    """获取数据库连接（自动建表）"""
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            symbol TEXT NOT NULL,
            name TEXT,
            score INTEGER,
            direction TEXT,
            price_at_signal REAL,
            regime TEXT,
            confidence INTEGER,
            reasons_bull TEXT,
            reasons_bear TEXT,
            conflicts TEXT,
            -- 后续回填
            price_5d REAL,
            price_10d REAL,
            price_20d REAL,
            pct_5d REAL,
            pct_10d REAL,
            pct_20d REAL,
            outcome_5d TEXT,
            outcome_10d TEXT,
            outcome_20d TEXT
        )
    """)
    conn.commit()
    return conn


def record_signal(
    symbol: str,
    name: str,
    score: int,
    direction: str,
    price: float,
    regime: str,
    confidence: int,
    reasons_bull: list[str],
    reasons_bear: list[str],
    conflicts: list[dict],
) -> int:
    """记录一次分析信号（同一 symbol 当天内不重复记录，score 变了则 UPDATE）"""
    conn = _get_conn()
    try:
        # 去重：同一 symbol 当天内只保留一条（以最新为准）
        today_start = _now_bj().replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        existing = conn.execute(
            "SELECT id, score FROM signals WHERE symbol = ? AND timestamp > ?",
            (symbol, today_start),
        ).fetchone()
        if existing:
            if existing["score"] == score:
                logger.info(f"⏭️ 信号去重: {symbol}(score={score}) 今日已记录 (#{existing['id']})")
                return existing["id"]
            # score 变了，UPDATE 而不是新增
            conn.execute(
                """UPDATE signals SET timestamp=?, score=?, direction=?, price_at_signal=?,
                   regime=?, confidence=?, reasons_bull=?, reasons_bear=?, conflicts=?
                   WHERE id=?""",
                (
                    _now_bj().isoformat(), score, direction, price, regime, confidence,
                    ", ".join(reasons_bull), ", ".join(reasons_bear),
                    "; ".join(c.get("description", "") for c in conflicts) if conflicts else "",
                    existing["id"],
                ),
            )
            conn.commit()
            logger.info(f"📝 信号已更新: #{existing['id']} {symbol} {direction}({score:+d}) @ {price} (旧score={existing['score']})")
            return existing["id"]

        cursor = conn.execute(
            """INSERT INTO signals 
               (timestamp, symbol, name, score, direction, price_at_signal, 
                regime, confidence, reasons_bull, reasons_bear, conflicts)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                _now_bj().isoformat(),
                symbol,
                name,
                score,
                direction,
                price,
                regime,
                confidence,
                ", ".join(reasons_bull),
                ", ".join(reasons_bear),
                "; ".join(c.get("description", "") for c in conflicts) if conflicts else "",
            ),
        )
        conn.commit()
        signal_id = cursor.lastrowid
        logger.info(f"📝 信号已记录: #{signal_id} {symbol} {direction}({score:+d}) @ {price}")
        return signal_id
    finally:
        conn.close()


def get_recent_signals(days: int = 7, symbol: Optional[str] = None) -> list[dict]:
    """获取最近N天的信号记录"""
    conn = _get_conn()
    try:
        cutoff = (_now_bj() - timedelta(days=days)).isoformat()
        if symbol:
            rows = conn.execute(
                "SELECT * FROM signals WHERE timestamp > ? AND symbol = ? ORDER BY timestamp DESC",
                (cutoff, symbol),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM signals WHERE timestamp > ? ORDER BY timestamp DESC",
                (cutoff,),
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_signal_stats(days: int = 30) -> dict[str, Any]:
    """计算信号统计：胜率、方向分布等"""
    conn = _get_conn()
    try:
        cutoff = (_now_bj() - timedelta(days=days)).isoformat()

        # 总信号数
        total = conn.execute(
            "SELECT COUNT(*) FROM signals WHERE timestamp > ?", (cutoff,)
        ).fetchone()[0]

        # 方向分布
        direction_dist = {}
        for row in conn.execute(
            "SELECT direction, COUNT(*) as cnt FROM signals WHERE timestamp > ? GROUP BY direction",
            (cutoff,),
        ):
            direction_dist[row["direction"]] = row["cnt"]

        # 已回填的胜率统计
        win_stats = {}
        for period, col_pct, col_outcome in [
            ("5d", "pct_5d", "outcome_5d"),
            ("10d", "pct_10d", "outcome_10d"),
            ("20d", "pct_20d", "outcome_20d"),
        ]:
            filled = conn.execute(
                f"SELECT COUNT(*) FROM signals WHERE timestamp > ? AND {col_pct} IS NOT NULL",
                (cutoff,),
            ).fetchone()[0]
            correct = conn.execute(
                f"SELECT COUNT(*) FROM signals WHERE timestamp > ? AND {col_outcome} = 'correct'",
                (cutoff,),
            ).fetchone()[0]
            win_stats[period] = {
                "total_filled": filled,
                "correct": correct,
                "win_rate": round(correct / filled * 100, 1) if filled > 0 else None,
            }

        # 最近分析的股票（行为护栏用）
        recent_symbols = [
            r["symbol"]
            for r in conn.execute(
                "SELECT symbol FROM signals WHERE timestamp > ? ORDER BY timestamp DESC LIMIT 20",
                ((_now_bj() - timedelta(hours=24)).isoformat(),),
            ).fetchall()
        ]

        # 今日分析次数
        today_start = _now_bj().replace(hour=0, minute=0, second=0).isoformat()
        today_count = conn.execute(
            "SELECT COUNT(*) FROM signals WHERE timestamp > ?", (today_start,)
        ).fetchone()[0]

        return {
            "period_days": days,
            "total_signals": total,
            "direction_distribution": direction_dist,
            "win_stats": win_stats,
            "today_count": today_count,
            "recent_24h_symbols": recent_symbols,
        }
    finally:
        conn.close()


def update_outcomes(market_data) -> dict[str, Any]:
    """
    回填历史信号的实际走势

    对所有未回填的信号，检查是否已过5/10/20个交易日，
    如果是则获取当时的价格并计算涨跌幅和正确性。

    Args:
        market_data: MarketData 实例，用于获取历史价格

    Returns:
        更新统计
    """
    conn = _get_conn()
    updated = {"5d": 0, "10d": 0, "20d": 0, "errors": 0}

    try:
        # ── Layer 1: 非交易日快速跳过 ──
        # 周末 A 股休市 + baostock 服务端不响应，直接 skip 避免卡死
        now = _now_bj()
        weekday = now.weekday()  # 0=Mon ... 6=Sun
        if weekday >= 5:
            return {
                **updated,
                "message": f"非交易日（{'周六' if weekday == 5 else '周日'}），跳过回填",
                "skipped": True,
            }

        # 盘中也不回填（9:00~14:xx 期间数据不稳定），盘后才有意义
        # A 股 15:00 收盘，cron 15:30 跑，hour=15 时已收盘，不拦
        hour = now.hour
        if 9 <= hour < 15:
            return {
                **updated,
                "message": f"盘中时段（{hour}:xx），建议收盘后回填",
                "skipped": True,
            }

        # 快速前置检查：最早的未回填信号是否有足够的交易日
        oldest_pending = conn.execute(
            """SELECT MIN(timestamp) FROM signals 
               WHERE price_5d IS NULL AND price_at_signal IS NOT NULL AND price_at_signal > 0"""
        ).fetchone()[0]

        if oldest_pending:
            oldest_date = datetime.fromisoformat(oldest_pending)
            cal_days = (now - oldest_date).days
            # 日历天 < 6 时不可能有 5 个交易日（最短：周一信号→下周一回填=7天，但周内信号可能 6 天就够）
            if cal_days < 6:
                return {
                    **updated,
                    "message": f"回填跳过: 最早待回填信号距今{cal_days}天，交易日不足5天，请工作日盘后再试",
                    "skipped": True,
                }

        # 获取所有有待回填的信号
        pending = conn.execute(
            """SELECT id, symbol, timestamp, score, direction, price_at_signal,
                      price_5d, price_10d, price_20d
               FROM signals 
               WHERE price_5d IS NULL OR price_10d IS NULL OR price_20d IS NULL
               ORDER BY timestamp ASC"""
        ).fetchall()

        # ── Layer 2: 按 symbol 缓存 K 线，避免重复查询 ──
        # 68 条信号可能只有 11 个不同 symbol，每个只查一次
        from .data import get_stock_daily_for_backfill
        _kline_cache: dict[str, Any] = {}  # {(symbol, days): df}

        def _get_kline_cached(symbol: str, days: int):
            """缓存 K 线查询，同 symbol 取最大 days 的缓存"""
            # 查找是否已有该 symbol 的缓存（取 days 最大的）
            for (cached_sym, cached_days), cached_df in _kline_cache.items():
                if cached_sym == symbol and cached_days >= days:
                    return cached_df
            # 没缓存，查询并存入
            df = get_stock_daily_for_backfill(symbol, days=days)
            _kline_cache[(symbol, days)] = df
            return df

        # 预计算每个 symbol 需要的最大 days，一次查询覆盖所有信号
        symbol_max_days: dict[str, int] = {}
        for row in pending:
            symbol = row["symbol"]
            signal_date = datetime.fromisoformat(row["timestamp"])
            days_elapsed = (now - signal_date).days
            needed = days_elapsed + 5
            if symbol not in symbol_max_days or needed > symbol_max_days[symbol]:
                symbol_max_days[symbol] = needed

        # 预热缓存：每个 symbol 只查一次（最大时间范围）
        for symbol, max_days in symbol_max_days.items():
            try:
                _kline_cache[(symbol, max_days)] = get_stock_daily_for_backfill(symbol, days=max_days)
            except Exception as e:
                logger.warning(f"预热缓存 {symbol} 失败: {e}")
            import time as _time
            _time.sleep(0.05)  # 让出 baostock 锁

        logger.info(f"📦 K 线缓存预热完成: {len(symbol_max_days)} 个 symbol（{len(pending)} 条信号）")

        for row in pending:
            signal_date = datetime.fromisoformat(row["timestamp"])
            days_elapsed = (now - signal_date).days
            symbol = row["symbol"]
            price_at = row["price_at_signal"]
            score = row["score"]

            if price_at is None or price_at == 0:
                continue

            # 日历天不足 6 天的信号跳过（不可能有 5 个交易日）
            if days_elapsed < 6 and row["price_5d"] is None:
                continue

            try:
                df = _get_kline_cached(symbol, days=days_elapsed + 5)
                if df is None or df.empty:
                    continue

                # 找信号日期之后的第N个交易日
                # 注意：A股和美股交易日不同，但 iloc[4] 取的是各自市场的第5个交易日，
                # 这是正确的——美股用美股交易日，A股用A股交易日。
                signal_dt = signal_date.date()
                # 按日期去重（baostock 偶发重复行，会膨胀 future 导致 20d 错填）
                df = df.drop_duplicates(subset=["date"]).reset_index(drop=True)
                future = df[df["date"].dt.date > signal_dt]

                updates = {}

                # 5日回填
                if row["price_5d"] is None and len(future) >= 5:
                    p5 = float(future.iloc[4]["close"])
                    pct5 = (p5 / price_at - 1) * 100
                    # v3 sanity check: 偏差>50%跳过（防脏数据，如赛力斯 id=82 的 -90.56%）
                    if abs(pct5) > 50:
                        logger.warning(f"⚠️ 5d回填异常跳过: {symbol} price_at={price_at} p5={p5} pct={pct5:.1f}%")
                    else:
                        outcome5 = _judge_outcome(score, pct5)
                        updates["price_5d"] = p5
                        updates["pct_5d"] = round(pct5, 2)
                        updates["outcome_5d"] = outcome5
                        updated["5d"] += 1

                # 10日回填（至少 14 日历天才可能有 10 个交易日）
                if row["price_10d"] is None and days_elapsed >= 14 and len(future) >= 10:
                    p10 = float(future.iloc[9]["close"])
                    pct10 = (p10 / price_at - 1) * 100
                    if abs(pct10) > 50:
                        logger.warning(f"⚠️ 10d回填异常跳过: {symbol} price_at={price_at} p10={p10} pct={pct10:.1f}%")
                    else:
                        outcome10 = _judge_outcome(score, pct10)
                        updates["price_10d"] = p10
                        updates["pct_10d"] = round(pct10, 2)
                        updates["outcome_10d"] = outcome10
                        updated["10d"] += 1

                # 20日回填（至少 28 日历天才可能有 20 个交易日）
                if row["price_20d"] is None and days_elapsed >= 28 and len(future) >= 20:
                    p20 = float(future.iloc[19]["close"])
                    pct20 = (p20 / price_at - 1) * 100
                    if abs(pct20) > 50:
                        logger.warning(f"⚠️ 20d回填异常跳过: {symbol} price_at={price_at} p20={p20} pct={pct20:.1f}%")
                    else:
                        outcome20 = _judge_outcome(score, pct20)
                        updates["price_20d"] = p20
                        updates["pct_20d"] = round(pct20, 2)
                        updates["outcome_20d"] = outcome20
                        updated["20d"] += 1

                if updates:
                    set_clause = ", ".join(f"{k} = ?" for k in updates)
                    values = list(updates.values()) + [row["id"]]
                    conn.execute(
                        f"UPDATE signals SET {set_clause} WHERE id = ?", values
                    )

            except Exception as e:
                logger.warning(f"回填 {symbol} #{row['id']} 失败: {e}")
                updated["errors"] += 1

        conn.commit()
        logger.info(f"📊 信号回填完成: 5d={updated['5d']}, 10d={updated['10d']}, 20d={updated['20d']}")
        return updated
    finally:
        conn.close()


def _judge_outcome(score: int, pct_change: float) -> str:
    """
    判断信号是否正确

    规则：
    - 看多(score>0) + 实际涨了 → correct
    - 看空(score<0) + 实际跌了 → correct
    - 中性(score≈0) + 波动<3% → correct（本来就没给方向）
    - 其他 → wrong
    """
    if score > 8:
        return "correct" if pct_change > 0 else "wrong"
    elif score < -8:
        return "correct" if pct_change < 0 else "wrong"
    else:
        # 中性信号，只要没大涨大跌就算对
        return "correct" if abs(pct_change) < 3 else "wrong"
