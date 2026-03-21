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
import yfinance as yf

logger = logging.getLogger(__name__)

# baostock 全局连接管理（单连接 + 线程锁）
import threading
_bs_lock = threading.Lock()
_bs_logged_in = False
_bs_login_time = 0
_BS_SESSION_TTL = 30  # 每 30 秒重新登录保持连接新鲜
_BS_SOCKET_TIMEOUT = 10  # baostock socket 超时（秒），防止僵尸 TCP 卡死进程


def _force_close_bs_socket():
    """强制关闭 baostock 内部 socket + 标记 session 失效，打断僵尸 TCP 连接"""
    global _bs_logged_in
    _bs_logged_in = False
    try:
        import baostock.common.context as bs_ctx
        if hasattr(bs_ctx, "default_socket"):
            old_sock = getattr(bs_ctx, "default_socket")
            if old_sock is not None:
                try:
                    old_sock.close()
                except Exception:
                    pass
                setattr(bs_ctx, "default_socket", None)
    except Exception:
        pass


def _patch_bs_socket_timeout():
    """给 baostock 内部 socket 注入超时，防止 recv/send 永久阻塞"""
    try:
        import baostock.common.context as bs_ctx
        if hasattr(bs_ctx, "default_socket"):
            sock = getattr(bs_ctx, "default_socket")
            if sock is not None:
                sock.settimeout(_BS_SOCKET_TIMEOUT)
    except Exception:
        pass


# Monkey-patch baostock SocketUtil.connect — 给新建 socket 自动设超时
try:
    import baostock.util.socketutil as _bs_sockutil
    import baostock.common.context as _bs_context
    import baostock.common.contants as _bs_cons
    import socket as _socket

    _orig_su_connect = _bs_sockutil.SocketUtil.connect

    def _connect_with_timeout(self):
        """baostock connect with socket timeout (防僵尸 TCP)"""
        sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        sock.settimeout(_BS_SOCKET_TIMEOUT)
        try:
            sock.connect((_bs_cons.BAOSTOCK_SERVER_IP, _bs_cons.BAOSTOCK_SERVER_PORT))
        except Exception:
            sock.close()  # 防 socket 泄漏
            logger.error("baostock socket connect 超时或失败")
            raise
        setattr(_bs_context, "default_socket", sock)

    _bs_sockutil.SocketUtil.connect = _connect_with_timeout
    logger.info("baostock SocketUtil.connect 已注入 %ds 超时保护", _BS_SOCKET_TIMEOUT)
except Exception as e:
    logger.warning(f"baostock monkey-patch 失败（不影响功能，但无超时保护）: {e}")


def _bs_login():
    """确保 baostock 已登录（超过 30s 自动重连，带 socket 超时保护）"""
    global _bs_logged_in, _bs_login_time
    now = time.time()
    if _bs_logged_in and (now - _bs_login_time) < _BS_SESSION_TTL:
        return  # 连接还新鲜

    # 强制关闭旧 socket + 标记 session 失效（打断可能的僵尸 TCP 连接）
    _force_close_bs_socket()

    import io, sys
    old_stdout = sys.stdout
    sys.stdout = io.StringIO()
    try:
        lg = bs.login()
    finally:
        sys.stdout = old_stdout

    # 登录后确保 socket 有超时（双保险：connect 已设，这里再设一次）
    _patch_bs_socket_timeout()

    if lg.error_code == "0":
        _bs_logged_in = True
        _bs_login_time = now
    else:
        _bs_logged_in = False
        logger.error(f"baostock 登录失败: {lg.error_msg}")


def _to_bs_code(symbol: str) -> str:
    """纯数字代码 → baostock 格式 (sz.002111 / sh.600519)"""
    if symbol.startswith(("6", "5")):
        return f"sh.{symbol}"
    return f"sz.{symbol}"


def is_us_stock(symbol: str) -> bool:
    """判断是否为美股代码（字母=美股，数字=A股）"""
    return bool(symbol) and symbol[0].isalpha()


class MarketData:
    """A 股 + 美股市场数据获取层"""

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
        return not is_us_stock(symbol) and symbol[:2] in ("51", "15", "16", "56", "58") and len(symbol) == 6

    # ========================
    # 美股数据 (yfinance)
    # ========================

    def get_us_stock_daily(self, symbol: str, days: int = 120) -> pd.DataFrame:
        """获取美股日K线 (yfinance)"""
        try:
            ticker = yf.Ticker(symbol)
            start = (datetime.now() - timedelta(days=days * 2)).strftime("%Y-%m-%d")
            df = ticker.history(start=start)
            if df.empty:
                logger.warning(f"yfinance {symbol}: 无数据")
                return pd.DataFrame()
            df = df.reset_index()
            df = df.rename(columns={
                "Date": "date", "Open": "open", "Close": "close",
                "High": "high", "Low": "low", "Volume": "volume",
            })
            df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None)
            df["pct_change"] = df["close"].pct_change() * 100
            df = df[["date", "open", "high", "low", "close", "volume", "pct_change"]]
            return df.dropna(subset=["close"]).tail(days).reset_index(drop=True)
        except Exception as e:
            logger.error(f"yfinance {symbol} 失败: {e}")
            return pd.DataFrame()

    _us_name_cache: dict[str, str] = {}

    def get_us_stock_name(self, symbol: str) -> str:
        """获取美股名称（内存缓存，避免重复 HTTP 请求）"""
        if symbol in self._us_name_cache:
            return self._us_name_cache[symbol]
        try:
            ticker = yf.Ticker(symbol)
            info = ticker.info
            name = info.get("shortName") or info.get("longName") or symbol
            self._us_name_cache[symbol] = name
            return name
        except Exception:
            return symbol

    def get_us_stock_news(self, symbol: str, limit: int = 5) -> list[dict]:
        """获取美股新闻（yfinance）"""
        try:
            ticker = yf.Ticker(symbol)
            raw_news = ticker.news or []
            news_list = []
            for item in raw_news[:limit]:
                # yfinance 新版: title 在 content.title 里
                content = item.get("content", {}) if isinstance(item.get("content"), dict) else {}
                title = content.get("title") or item.get("title", "")
                publisher = (content.get("provider", {}) or {}).get("displayName") or item.get("publisher", "")
                if title:
                    news_list.append({
                        "title": title,
                        "source": publisher,
                        "time": content.get("pubDate", ""),
                        "content": (content.get("summary") or "")[:200],
                    })
            return news_list
        except Exception as e:
            logger.warning(f"yfinance {symbol} 新闻获取失败: {e}")
            return []

    def get_us_index_daily(self, index_symbol: str = "SPY", days: int = 120) -> pd.DataFrame:
        """获取美股指数日K线（SPY 作为 S&P 500 代理）
        
        TODO: 如果 analyze_stock("SPY")，regime 会用 SPY 跟自己比（无 market context）。
        未来可加 SPY 特判或引入 VIX 做辅助体制识别。
        """
        return self.get_us_stock_daily(index_symbol, days)

    def get_stock_daily(
        self,
        symbol: str,
        days: int = 120,
        adjust: str = "qfq",
    ) -> pd.DataFrame:
        """
        获取个股/ETF 日K线数据
        美股: yfinance | A股: baostock 主力 + AKShare fallback
        """
        # 美股路由
        if is_us_stock(symbol):
            return self.get_us_stock_daily(symbol, days)
        # 优先用 baostock（快、无反爬），线程锁保护单连接
        try:
            acquired = _bs_lock.acquire(timeout=15)
            if not acquired:
                raise TimeoutError("baostock lock acquire timeout")
            try:
                _bs_login()
                bs_code = _to_bs_code(symbol)
                start = (datetime.now() - timedelta(days=days * 2)).strftime("%Y-%m-%d")
                end = datetime.now().strftime("%Y-%m-%d")
                adj_map = {"qfq": "2", "hfq": "1", "": "3"}
                adj_flag = adj_map.get(adjust, "2")

                rs = bs.query_history_k_data_plus(
                    bs_code,
                    "date,open,high,low,close,volume,pctChg,turn,peTTM,pbMRQ",
                    start_date=start,
                    end_date=end,
                    frequency="d",
                    adjustflag=adj_flag,
                )
                rows = []
                while (rs.error_code == "0") and rs.next():
                    rows.append(rs.get_row_data())
            finally:
                _bs_lock.release()

            if not rows:
                logger.debug(f"baostock {symbol}: 0 rows (可能停牌/退市)")

            if rows:
                df = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume", "pct_change", "turn", "pe_ttm", "pb_mrq"])
                for col in ["open", "high", "low", "close", "volume", "pct_change", "turn", "pe_ttm", "pb_mrq"]:
                    df[col] = pd.to_numeric(df[col], errors="coerce")
                df["date"] = pd.to_datetime(df["date"])
                df = df.dropna(subset=["close"]).sort_values("date").tail(days).reset_index(drop=True)
                return df
        except Exception as e:
            logger.warning(f"baostock {symbol} 异常: {e}")
            _force_close_bs_socket()

        # AKShare fallback（仅在 baostock 完全不可用时才尝试，如 ETF 特殊代码）
        if self._is_etf(symbol):
            end_str = datetime.now().strftime("%Y%m%d")
            start_str = (datetime.now() - timedelta(days=days * 2)).strftime("%Y%m%d")
            try:
                df = ak.fund_etf_hist_em(symbol=symbol, period="daily", start_date=start_str, end_date=end_str, adjust=adjust)
                df = df.rename(columns={
                    "日期": "date", "开盘": "open", "收盘": "close", "最高": "high",
                    "最低": "low", "成交量": "volume", "涨跌幅": "pct_change",
                })
                df["date"] = pd.to_datetime(df["date"])
                df = df.sort_values("date").tail(days).reset_index(drop=True)
                return df
            except Exception as e:
                logger.error(f"获取 ETF {symbol} 日K线失败: {e}")
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
        # 美股
        if is_us_stock(symbol):
            return self.get_us_stock_name(symbol)
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
        获取指数日K线
        美股指数 (SPY 等): yfinance | A股: baostock 主力 + AKShare fallback
        """
        if is_us_stock(index_code):
            return self.get_us_index_daily(index_code, days)
        # baostock（线程锁保护单连接）
        try:
            acquired = _bs_lock.acquire(timeout=15)
            if not acquired:
                raise TimeoutError("baostock lock acquire timeout")
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
            finally:
                _bs_lock.release()
            if rows:
                df = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume", "pct_change"])
                for col in ["open", "high", "low", "close", "volume", "pct_change"]:
                    df[col] = pd.to_numeric(df[col], errors="coerce")
                df["date"] = pd.to_datetime(df["date"])
                return df.tail(days).reset_index(drop=True)
        except Exception as e:
            logger.warning(f"baostock 指数 {index_code} 失败, fallback AKShare: {e}")
            _force_close_bs_socket()

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
        """获取个股近期龙虎榜数据（按近30天日期范围查全市场后筛个股）"""
        try:
            from datetime import datetime, timedelta
            end_date = datetime.now().strftime("%Y%m%d")
            start_date = (datetime.now() - timedelta(days=30)).strftime("%Y%m%d")
            df = ak.stock_lhb_detail_em(start_date=start_date, end_date=end_date)
            if df.empty:
                return []
            # 筛选当前股票（代码列可能叫"代码"或"证券代码"）
            code_col = None
            for col in ["代码", "证券代码", "股票代码"]:
                if col in df.columns:
                    code_col = col
                    break
            if not code_col:
                return []
            df = df[df[code_col].astype(str).str.strip() == symbol]
            if df.empty:
                return []
            records = []
            for _, row in df.head(10).iterrows():
                # 列名可能因 AKShare 版本不同而变化，做兼容
                date_val = row.get("上榜日期", row.get("日期", ""))
                reason_val = row.get("上榜原因", row.get("解读", ""))
                buy_val = row.get("买入总计", row.get("龙虎榜净买额", 0))
                sell_val = row.get("卖出总计", 0)
                net_val = row.get("净买入", row.get("龙虎榜净买额", 0))
                records.append({
                    "date": str(date_val),
                    "reason": str(reason_val),
                    "buy_total": float(buy_val) if buy_val else 0,
                    "sell_total": float(sell_val) if sell_val else 0,
                    "net": float(net_val) if net_val else 0,
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
        """获取个股相关新闻（美股: yfinance | A股: AKShare）"""
        if is_us_stock(symbol):
            return self.get_us_stock_news(symbol, limit)
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
