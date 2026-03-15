"""
数据层 — baostock (日K线) + AKShare (资金/新闻/龙虎榜)
baostock: 无反爬、无限流、无需注册，日K线主力数据源
AKShare: 资金流向、新闻、龙虎榜、北向资金等 baostock 不覆盖的接口
"""

import logging
import time
from datetime import datetime, timedelta
from typing import Optional

import akshare as ak
import baostock as bs
import pandas as pd

logger = logging.getLogger(__name__)

# baostock 全局连接管理
_bs_logged_in = False


def _bs_login():
    """确保 baostock 已登录（全局单连接）"""
    global _bs_logged_in
    if not _bs_logged_in:
        import io, sys
        # baostock login() prints to stdout, suppress it
        old_stdout = sys.stdout
        sys.stdout = io.StringIO()
        try:
            lg = bs.login()
        finally:
            sys.stdout = old_stdout
        if lg.error_code == "0":
            _bs_logged_in = True
            logger.info("baostock 登录成功")
        else:
            logger.error(f"baostock 登录失败: {lg.error_msg}")


def _to_bs_code(symbol: str) -> str:
    """纯数字代码 → baostock 格式 (sz.002111 / sh.600519)"""
    if symbol.startswith(("6", "5")):
        return f"sh.{symbol}"
    return f"sz.{symbol}"


class MarketData:
    """A 股市场数据获取层"""

    def __init__(self):
        # 全市场实时行情缓存
        self._spot_cache: Optional[pd.DataFrame] = None
        self._spot_cache_time: float = 0
        self._spot_cache_ttl: float = 30  # 缓存30秒
        self._spot_fail_until: float = 0  # 失败冷却期（60秒内不重试）

    def _get_spot_cache(self) -> pd.DataFrame:
        """获取全市场实时行情（带缓存+失败冷却）"""
        now = time.monotonic()
        # 有效缓存直接返回
        if self._spot_cache is not None and (now - self._spot_cache_time) < self._spot_cache_ttl:
            return self._spot_cache
        # 冷却期内不重试，直接返回过期缓存或空
        if now < self._spot_fail_until:
            return self._spot_cache if self._spot_cache is not None else pd.DataFrame()
        try:
            self._spot_cache = ak.stock_zh_a_spot_em()
            self._spot_cache_time = now
            self._spot_fail_until = 0  # 成功则清除冷却
        except Exception as e:
            logger.warning(f"全市场实时行情获取失败: {e}")
            self._spot_fail_until = now + 60  # 60秒内不再重试
            if self._spot_cache is not None:
                return self._spot_cache
            return pd.DataFrame()
        return self._spot_cache

    # ========================
    # 行情数据
    # ========================

    @staticmethod
    def _is_etf(symbol: str) -> bool:
        """判断是否为 ETF 代码（51xxxx/15xxxx/16xxxx/56xxxx/58xxxx）"""
        return symbol[:2] in ("51", "15", "16", "56", "58") and len(symbol) == 6

    def get_stock_daily(
        self,
        symbol: str,
        days: int = 120,
        adjust: str = "qfq",
    ) -> pd.DataFrame:
        """
        获取个股/ETF 日K线数据（baostock 主力 + AKShare fallback）

        Args:
            symbol: 股票或ETF代码（纯数字，如 "002111" 或 "512400"）
            days: 获取天数
            adjust: 复权方式 qfq=前复权, hfq=后复权, ""=不复权

        Returns:
            DataFrame with columns: date, open, close, high, low, volume, pct_change
        """
        # 优先用 baostock（快、无反爬）
        try:
            _bs_login()
            bs_code = _to_bs_code(symbol)
            start = (datetime.now() - timedelta(days=days * 2)).strftime("%Y-%m-%d")
            end = datetime.now().strftime("%Y-%m-%d")
            # baostock 复权映射
            adj_map = {"qfq": "2", "hfq": "1", "": "3"}
            adj_flag = adj_map.get(adjust, "2")

            rs = bs.query_history_k_data_plus(
                bs_code,
                "date,open,high,low,close,volume,pctChg",
                start_date=start,
                end_date=end,
                frequency="d",
                adjustflag=adj_flag,
            )
            rows = []
            while (rs.error_code == "0") and rs.next():
                rows.append(rs.get_row_data())

            if not rows:
                logger.debug(f"baostock {symbol} 返回 0 rows (error={rs.error_code} msg={rs.error_msg})")

            if rows:
                df = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume", "pct_change"])
                for col in ["open", "high", "low", "close", "volume", "pct_change"]:
                    df[col] = pd.to_numeric(df[col], errors="coerce")
                df["date"] = pd.to_datetime(df["date"])
                df = df.dropna(subset=["close"]).sort_values("date").tail(days).reset_index(drop=True)
                return df
        except Exception as e:
            logger.warning(f"baostock {symbol} 失败, fallback AKShare: {e}")

        # AKShare fallback
        end_str = datetime.now().strftime("%Y%m%d")
        start_str = (datetime.now() - timedelta(days=days * 2)).strftime("%Y%m%d")
        try:
            if self._is_etf(symbol):
                df = ak.fund_etf_hist_em(symbol=symbol, period="daily", start_date=start_str, end_date=end_str, adjust=adjust)
            else:
                df = ak.stock_zh_a_hist(symbol=symbol, period="daily", start_date=start_str, end_date=end_str, adjust=adjust)
            df = df.rename(columns={
                "日期": "date", "开盘": "open", "收盘": "close", "最高": "high",
                "最低": "low", "成交量": "volume", "涨跌幅": "pct_change",
            })
            df["date"] = pd.to_datetime(df["date"])
            df = df.sort_values("date").tail(days).reset_index(drop=True)
            return df
        except Exception as e:
            logger.error(f"获取 {symbol} 日K线失败 (baostock+AKShare): {e}")
            return pd.DataFrame()

    def get_stock_realtime(self, symbol: str) -> dict:
        """
        获取个股实时行情快照

        Returns:
            dict with: price, change, pct_change, volume, turnover, high, low, open, pre_close
            如果实时数据不可用，返回带 fallback 标记的空结果
        """
        try:
            df = self._get_spot_cache()
            if df.empty or "代码" not in df.columns:
                logger.warning(f"实时行情缓存为空，{symbol} 使用 fallback")
                return {"fallback": True, "code": symbol}

            row = df[df["代码"] == symbol]
            if row.empty:
                return {"fallback": True, "code": symbol}

            r = row.iloc[0]
            return {
                "code": symbol,
                "name": str(r.get("名称", "")),
                "price": float(r.get("最新价", 0)),
                "change": float(r.get("涨跌额", 0)),
                "pct_change": float(r.get("涨跌幅", 0)),
                "volume": float(r.get("成交量", 0)),
                "turnover": float(r.get("成交额", 0)),
                "high": float(r.get("最高", 0)),
                "low": float(r.get("最低", 0)),
                "open": float(r.get("今开", 0)),
                "pre_close": float(r.get("昨收", 0)),
                "total_mv": float(r.get("总市值", 0)),
                "circ_mv": float(r.get("流通市值", 0)),
                "pe": float(r.get("市盈率-动态", 0)),
                "pb": float(r.get("市净率", 0)),
            }
        except Exception as e:
            logger.error(f"获取实时行情失败: {e}")
            return {"fallback": True, "code": symbol}

    def get_stock_name(self, symbol: str) -> str:
        """根据代码获取股票/ETF名称"""
        # ETF 名称查询
        if self._is_etf(symbol):
            try:
                df = ak.fund_etf_spot_em()
                if not df.empty and "代码" in df.columns:
                    row = df[df["代码"] == symbol]
                    if not row.empty:
                        return str(row.iloc[0].get("名称", symbol))
            except Exception:
                pass
            return symbol
        # 个股名称查询
        try:
            df = self._get_spot_cache()
            if not df.empty and "代码" in df.columns:
                row = df[df["代码"] == symbol]
                if not row.empty:
                    return str(row.iloc[0].get("名称", symbol))
        except Exception:
            pass
        return symbol

    # ========================
    # 资金流向
    # ========================

    def get_money_flow(self, symbol: str) -> dict:
        """
        获取个股资金流向（主力、超大单、大单、中单、小单）

        Returns:
            dict with net inflow data
        """
        try:
            df = ak.stock_individual_fund_flow(stock=symbol, market="sh" if symbol.startswith("6") else "sz")
            if df.empty:
                return {"error": "无资金流向数据"}

            latest = df.iloc[-1]
            return {
                "date": str(latest.get("日期", "")),
                "main_net_inflow": float(latest.get("主力净流入-净额", 0)),
                "main_net_inflow_pct": float(latest.get("主力净流入-净占比", 0)),
                "super_large_net": float(latest.get("超大单净流入-净额", 0)),
                "large_net": float(latest.get("大单净流入-净额", 0)),
                "medium_net": float(latest.get("中单净流入-净额", 0)),
                "small_net": float(latest.get("小单净流入-净额", 0)),
            }
        except Exception as e:
            logger.error(f"获取资金流向失败: {e}")
            return {"error": str(e)}

    def get_money_flow_history(self, symbol: str, days: int = 20) -> pd.DataFrame:
        """获取近N日资金流向历史"""
        try:
            df = ak.stock_individual_fund_flow(stock=symbol, market="sh" if symbol.startswith("6") else "sz")
            df = df.rename(columns={
                "日期": "date",
                "主力净流入-净额": "main_net",
                "主力净流入-净占比": "main_pct",
                "超大单净流入-净额": "super_large",
                "大单净流入-净额": "large",
                "中单净流入-净额": "medium",
                "小单净流入-净额": "small",
            })
            return df.tail(days).reset_index(drop=True)
        except Exception as e:
            logger.error(f"获取资金流向历史失败: {e}")
            return pd.DataFrame()

    # ========================
    # 大盘指数
    # ========================

    def get_index_daily(self, index_code: str = "000001", days: int = 120) -> pd.DataFrame:
        """
        获取指数日K线（baostock 主力 + AKShare fallback）

        Args:
            index_code: 指数代码（000001=上证, 399001=深证, 399006=创业板）
        """
        # baostock
        try:
            _bs_login()
            bs_code = _to_bs_code(index_code)
            start = (datetime.now() - timedelta(days=days * 2)).strftime("%Y-%m-%d")
            end = datetime.now().strftime("%Y-%m-%d")
            rs = bs.query_history_k_data_plus(
                bs_code, "date,open,high,low,close,volume,pctChg",
                start_date=start, end_date=end, frequency="d",
            )
            rows = []
            while (rs.error_code == "0") and rs.next():
                rows.append(rs.get_row_data())
            if rows:
                df = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume", "pct_change"])
                for col in ["open", "high", "low", "close", "volume", "pct_change"]:
                    df[col] = pd.to_numeric(df[col], errors="coerce")
                df["date"] = pd.to_datetime(df["date"])
                return df.tail(days).reset_index(drop=True)
        except Exception as e:
            logger.warning(f"baostock 指数 {index_code} 失败, fallback AKShare: {e}")

        # AKShare fallback
        try:
            prefix = "sh" if index_code.startswith("0") else "sz"
            df = ak.stock_zh_index_daily_em(symbol=f"{prefix}{index_code}")
            df = df.rename(columns={"date": "date", "open": "open", "close": "close", "high": "high", "low": "low", "volume": "volume"})
            df["date"] = pd.to_datetime(df["date"])
            return df.tail(days).reset_index(drop=True)
        except Exception as e:
            logger.error(f"获取指数 {index_code} 失败 (baostock+AKShare): {e}")
            return pd.DataFrame()

    # ========================
    # 龙虎榜
    # ========================

    def get_lhb(self, symbol: str) -> list[dict]:
        """获取个股近期龙虎榜数据"""
        try:
            df = ak.stock_lhb_detail_em(symbol=symbol, date="近一月")
            if df.empty:
                return []
            records = []
            for _, row in df.head(10).iterrows():
                records.append({
                    "date": str(row.get("上榜日期", "")),
                    "reason": str(row.get("上榜原因", "")),
                    "buy_total": float(row.get("买入总计", 0)),
                    "sell_total": float(row.get("卖出总计", 0)),
                    "net": float(row.get("净买入", 0)),
                })
            return records
        except Exception as e:
            logger.warning(f"龙虎榜数据获取失败: {e}")
            return []

    # ========================
    # 北向资金
    # ========================

    def get_north_flow(self, days: int = 20) -> pd.DataFrame:
        """获取北向资金近N日净流入"""
        try:
            df = ak.stock_hsgt_north_net_flow_in_em(symbol="北上")
            df = df.rename(columns={
                "date": "date",
                "value": "net_inflow",
            })
            return df.tail(days).reset_index(drop=True)
        except Exception as e:
            logger.error(f"获取北向资金失败: {e}")
            return pd.DataFrame()

    # ========================
    # 新闻/公告
    # ========================

    def get_stock_news(self, symbol: str, limit: int = 10) -> list[dict]:
        """获取个股相关新闻"""
        try:
            df = ak.stock_news_em(symbol=symbol)
            news_list = []
            for _, row in df.head(limit).iterrows():
                news_list.append({
                    "title": str(row.get("新闻标题", "")),
                    "content": str(row.get("新闻内容", ""))[:200],
                    "time": str(row.get("发布时间", "")),
                    "source": str(row.get("文章来源", "")),
                })
            return news_list
        except Exception as e:
            logger.warning(f"获取新闻失败: {e}")
            return []
