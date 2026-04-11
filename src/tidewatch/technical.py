"""
技术分析引擎 — Technical Analysis Engine
计算技术指标 + 形态识别 + 信号生成
"""

import logging
from typing import Any

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


class TechnicalAnalyzer:
    """技术分析引擎：量价指标 + 形态识别"""

    def analyze(self, df: pd.DataFrame) -> dict[str, Any]:
        """
        对日K线数据做完整技术分析

        Args:
            df: 日K线 DataFrame (需含 date, open, close, high, low, volume)

        Returns:
            dict: 包含各类技术指标和信号
        """
        if df.empty or len(df) < 20:
            return {"error": "数据不足，至少需要20个交易日"}

        result = {}
        result["ma"] = self._calc_moving_averages(df)
        result["volume"] = self._calc_volume_indicators(df)
        result["momentum"] = self._calc_momentum(df)
        result["volatility"] = self._calc_volatility(df)
        result["price_position"] = self._calc_price_position(df)
        result["patterns"] = self._detect_patterns(df)
        result["trend"] = self._assess_trend(df, result)

        return result

    def _calc_moving_averages(self, df: pd.DataFrame) -> dict:
        """均线系统"""
        close = df["close"]
        latest = close.iloc[-1]

        ma5 = close.rolling(5).mean().iloc[-1]
        ma10 = close.rolling(10).mean().iloc[-1]
        ma20 = close.rolling(20).mean().iloc[-1]
        ma60 = close.rolling(60).mean().iloc[-1] if len(df) >= 60 else None

        # 均线排列判断
        if ma60 is not None:
            bullish_aligned = ma5 > ma10 > ma20 > ma60
            bearish_aligned = ma5 < ma10 < ma20 < ma60
        else:
            bullish_aligned = ma5 > ma10 > ma20
            bearish_aligned = ma5 < ma10 < ma20

        return {
            "ma5": round(ma5, 2),
            "ma10": round(ma10, 2),
            "ma20": round(ma20, 2),
            "ma60": round(ma60, 2) if ma60 else None,
            "price_vs_ma5": round((latest / ma5 - 1) * 100, 2),
            "price_vs_ma20": round((latest / ma20 - 1) * 100, 2),
            "bullish_aligned": bullish_aligned,
            "bearish_aligned": bearish_aligned,
            "ma5_slope": round((ma5 - close.rolling(5).mean().iloc[-3]) / ma5 * 100, 3),
        }

    def _calc_volume_indicators(self, df: pd.DataFrame) -> dict:
        """量能指标"""
        vol = df["volume"]
        close = df["close"]

        avg_vol_5 = vol.rolling(5).mean().iloc[-1]
        avg_vol_20 = vol.rolling(20).mean().iloc[-1]
        latest_vol = vol.iloc[-1]

        # OBV (On Balance Volume)
        obv = (np.sign(close.diff()) * vol).cumsum()
        obv_slope = (obv.iloc[-1] - obv.iloc[-5]) / abs(obv.iloc[-5]) if obv.iloc[-5] != 0 else 0

        # 量比
        volume_ratio = latest_vol / avg_vol_5 if avg_vol_5 > 0 else 0

        # 换手率（baostock turn 字段，百分比）
        turn_rate = float(df["turn"].iloc[-1]) if "turn" in df.columns and pd.notna(df["turn"].iloc[-1]) else 0
        avg_turn_5 = float(df["turn"].rolling(5).mean().iloc[-1]) if "turn" in df.columns else 0

        # VWAP 近似 (当日不可用时用近5日均价)
        if "turnover" in df.columns and df["turnover"].iloc[-1] > 0:
            vwap = df["turnover"].iloc[-1] / df["volume"].iloc[-1] if df["volume"].iloc[-1] > 0 else close.iloc[-1]
        else:
            vwap = (close * vol).rolling(5).sum().iloc[-1] / vol.rolling(5).sum().iloc[-1]

        return {
            "latest_volume": int(latest_vol),
            "avg_volume_5d": int(avg_vol_5),
            "avg_volume_20d": int(avg_vol_20),
            "volume_ratio": round(volume_ratio, 2),
            "obv_slope": round(obv_slope, 4),
            "vwap": round(vwap, 2),
            "price_vs_vwap": round((close.iloc[-1] / vwap - 1) * 100, 2),
            "shrinking": volume_ratio < 0.7,
            "expanding": volume_ratio > 1.5,
            "turn_rate": round(turn_rate, 2),
            "avg_turn_5d": round(avg_turn_5, 2),
        }

    def _calc_momentum(self, df: pd.DataFrame) -> dict:
        """动量指标"""
        close = df["close"]
        high = df["high"]
        low = df["low"]

        # RSI (14日)
        delta = close.diff()
        gain = delta.clip(lower=0).rolling(14).mean()
        loss = (-delta.clip(upper=0)).rolling(14).mean()
        rs = gain.iloc[-1] / loss.iloc[-1] if loss.iloc[-1] != 0 else 100
        rsi = 100 - (100 / (1 + rs))

        # MACD
        ema12 = close.ewm(span=12).mean()
        ema26 = close.ewm(span=26).mean()
        dif = ema12 - ema26
        dea = dif.ewm(span=9).mean()
        macd_bar = (dif - dea) * 2

        # KDJ
        low_9 = low.rolling(9).min()
        high_9 = high.rolling(9).max()
        rsv = (close - low_9) / (high_9 - low_9) * 100
        rsv = rsv.fillna(50)
        k = rsv.ewm(com=2).mean()
        d = k.ewm(com=2).mean()
        j = 3 * k - 2 * d

        return {
            "rsi_14": round(rsi, 1),
            "rsi_signal": "超买" if rsi > 70 else ("超卖" if rsi < 30 else "中性"),
            "macd_dif": round(dif.iloc[-1], 3),
            "macd_dea": round(dea.iloc[-1], 3),
            "macd_bar": round(macd_bar.iloc[-1], 3),
            "macd_cross": "金叉" if dif.iloc[-1] > dea.iloc[-1] and dif.iloc[-2] <= dea.iloc[-2] else
                          ("死叉" if dif.iloc[-1] < dea.iloc[-1] and dif.iloc[-2] >= dea.iloc[-2] else "无"),
            "kdj_k": round(k.iloc[-1], 1),
            "kdj_d": round(d.iloc[-1], 1),
            "kdj_j": round(j.iloc[-1], 1),
        }

    def _calc_volatility(self, df: pd.DataFrame) -> dict:
        """波动率指标"""
        close = df["close"]
        high = df["high"]
        low = df["low"]

        # ATR (14日)
        tr = pd.concat([
            high - low,
            (high - close.shift()).abs(),
            (low - close.shift()).abs(),
        ], axis=1).max(axis=1)
        atr = tr.rolling(14).mean().iloc[-1]

        # 布林带 (20日)
        sma20 = close.rolling(20).mean().iloc[-1]
        std20 = close.rolling(20).std().iloc[-1]
        boll_upper = sma20 + 2 * std20
        boll_lower = sma20 - 2 * std20
        boll_width = (boll_upper - boll_lower) / sma20 * 100

        # 价格在布林带中的位置 (0=下轨, 100=上轨)
        price = close.iloc[-1]
        boll_position = (price - boll_lower) / (boll_upper - boll_lower) * 100 if (boll_upper - boll_lower) > 0 else 50

        # 20日波动率
        returns_20d = close.pct_change().tail(20).std() * np.sqrt(252) * 100

        return {
            "atr_14": round(atr, 2),
            "atr_pct": round(atr / price * 100, 2),
            "boll_upper": round(boll_upper, 2),
            "boll_mid": round(sma20, 2),
            "boll_lower": round(boll_lower, 2),
            "boll_width": round(boll_width, 2),
            "boll_position": round(boll_position, 1),
            "volatility_20d": round(returns_20d, 1),
        }

    def _calc_price_position(self, df: pd.DataFrame) -> dict:
        """价格位置分析"""
        close = df["close"]
        price = close.iloc[-1]

        high_20 = df["high"].tail(20).max()
        low_20 = df["low"].tail(20).min()
        pos_20 = (price - low_20) / (high_20 - low_20) * 100 if (high_20 - low_20) > 0 else 50

        high_60 = df["high"].tail(60).max() if len(df) >= 60 else df["high"].max()
        low_60 = df["low"].tail(60).min() if len(df) >= 60 else df["low"].min()
        pos_60 = (price - low_60) / (high_60 - low_60) * 100 if (high_60 - low_60) > 0 else 50

        # 近期涨跌幅
        pct_5d = (price / close.iloc[-6] - 1) * 100 if len(close) >= 6 else 0
        pct_20d = (price / close.iloc[-21] - 1) * 100 if len(close) >= 21 else 0

        return {
            "price": round(price, 2),
            "position_20d": round(pos_20, 1),
            "position_60d": round(pos_60, 1),
            "high_20d": round(high_20, 2),
            "low_20d": round(low_20, 2),
            "pct_5d": round(pct_5d, 2),
            "pct_20d": round(pct_20d, 2),
            "near_20d_high": pos_20 > 90,
            "near_20d_low": pos_20 < 10,
        }

    def _detect_patterns(self, df: pd.DataFrame) -> list[str]:
        """检测常见K线形态"""
        patterns = []
        close = df["close"]
        open_ = df["open"]
        high = df["high"]
        low = df["low"]
        vol = df["volume"]

        # 最近一根K线
        c, o, h, l = close.iloc[-1], open_.iloc[-1], high.iloc[-1], low.iloc[-1]
        body = abs(c - o)
        upper_shadow = h - max(c, o)
        lower_shadow = min(c, o) - l

        # 十字星
        if body < (h - l) * 0.1 and (h - l) > 0:
            patterns.append("十字星")

        # 长下影线 (锤子线)
        if lower_shadow > body * 2 and upper_shadow < body * 0.5 and body > 0:
            patterns.append("长下影线(锤子)")

        # 长上影线
        if upper_shadow > body * 2 and lower_shadow < body * 0.5 and body > 0:
            patterns.append("长上影线(射击之星)")

        # 放量突破
        vol_ratio = vol.iloc[-1] / vol.rolling(20).mean().iloc[-1] if vol.rolling(20).mean().iloc[-1] > 0 else 1
        ma20 = close.rolling(20).mean().iloc[-1]
        if c > ma20 and close.iloc[-2] < close.rolling(20).mean().iloc[-2] and vol_ratio > 1.5:
            patterns.append("放量突破MA20")

        # 缩量回调
        if c < close.iloc[-2] and vol.iloc[-1] < vol.rolling(5).mean().iloc[-1] * 0.7:
            patterns.append("缩量回调")

        # 连续上涨/下跌
        consecutive_up = all(close.iloc[i] > close.iloc[i-1] for i in range(-3, 0))
        consecutive_down = all(close.iloc[i] < close.iloc[i-1] for i in range(-3, 0))
        if consecutive_up:
            patterns.append("三连阳")
        if consecutive_down:
            patterns.append("三连阴")

        return patterns if patterns else ["无明显形态"]

    def _assess_trend(self, df: pd.DataFrame, indicators: dict) -> dict:
        """综合趋势评估 — 多维度加权评分"""
        ma = indicators["ma"]
        mom = indicators["momentum"]
        vol = indicators["volume"]
        boll = indicators["volatility"]
        pos = indicators["price_position"]
        patterns = indicators.get("patterns", [])

        score = 0
        reasons_bull = []
        reasons_bear = []

        # ── 均线系统 (±25) ──
        if ma["bullish_aligned"]:
            score += 25
            reasons_bull.append("均线多头排列")
        elif ma["bearish_aligned"]:
            score -= 25
            reasons_bear.append("均线空头排列")
        else:
            # 非完美排列时看 MA5 斜率方向（v3: +8→+4，回填含MA5上行仅50%胜率）
            if ma["ma5_slope"] > 0.3:
                score += 4
                reasons_bull.append(f"MA5上行({ma['ma5_slope']:+.1f}%)")
            elif ma["ma5_slope"] < -0.3:
                score -= 8
                reasons_bear.append(f"MA5下行({ma['ma5_slope']:+.1f}%)")

        # 价格 vs MA5 (±8)
        if ma["price_vs_ma5"] > 0.5:
            score += 8
            reasons_bull.append(f"站上MA5({ma['price_vs_ma5']:+.1f}%)")
        elif ma["price_vs_ma5"] < -0.5:
            score -= 8
            reasons_bear.append(f"跌破MA5({ma['price_vs_ma5']:+.1f}%)")

        # 价格 vs MA20 (±8)
        if ma["price_vs_ma20"] > 2:
            score += 8
            reasons_bull.append(f"站稳MA20上方({ma['price_vs_ma20']:+.1f}%)")
        elif ma["price_vs_ma20"] < -2:
            score -= 8
            reasons_bear.append(f"远离MA20({ma['price_vs_ma20']:+.1f}%)")

        # ── 动量指标 (±25) ──
        # RSI
        if mom["rsi_14"] > 65:
            score += 8
            reasons_bull.append(f"RSI偏强({mom['rsi_14']:.0f})")
        elif mom["rsi_14"] > 50:
            score += 4
        elif mom["rsi_14"] < 35:
            score -= 8
            reasons_bear.append(f"RSI偏弱({mom['rsi_14']:.0f})")
        elif mom["rsi_14"] < 50:
            score -= 4

        if mom["rsi_14"] > 80:
            score -= 5
            reasons_bear.append("RSI超买警告")
        elif mom["rsi_14"] < 20:
            score += 5
            reasons_bull.append("RSI超卖反弹机会")

        # MACD
        if mom["macd_bar"] > 0:
            score += 8
            if mom["macd_bar"] > abs(mom.get("macd_dea", 1)) * 0.5:
                score += 4
                reasons_bull.append("MACD红柱放大")
            else:
                reasons_bull.append("MACD红柱")
        else:
            score -= 8
            if abs(mom["macd_bar"]) > abs(mom.get("macd_dea", 1)) * 0.5:
                score -= 4
                reasons_bear.append("MACD绿柱放大")
            else:
                reasons_bear.append("MACD绿柱")

        if mom["macd_cross"] == "金叉":
            score += 15
            reasons_bull.append("MACD金叉!")
        elif mom["macd_cross"] == "死叉":
            score -= 15
            reasons_bear.append("MACD死叉!")

        # KDJ
        if mom["kdj_j"] > 80:
            score += 5
            reasons_bull.append(f"KDJ强势(J={mom['kdj_j']:.0f})")
        elif mom["kdj_j"] < 20:
            score -= 5
            reasons_bear.append(f"KDJ弱势(J={mom['kdj_j']:.0f})")

        # ── 量能 (±15) ──
        if vol["obv_slope"] > 0.02:
            score += 8
            reasons_bull.append("OBV上升(资金流入)")
        elif vol["obv_slope"] < -0.02:
            score -= 8
            reasons_bear.append("OBV下降(资金流出)")

        if vol["volume_ratio"] > 1.5:
            # 放量要看方向
            if pos["pct_5d"] > 0:
                score += 7
                reasons_bull.append(f"放量上攻(量比{vol['volume_ratio']:.1f}x)")
            else:
                score -= 7
                reasons_bear.append(f"放量下跌(量比{vol['volume_ratio']:.1f}x)")
        elif vol["volume_ratio"] < 0.6:
            reasons_bear.append(f"极度缩量(量比{vol['volume_ratio']:.1f}x)")

        # 换手率异常检测（与量比+OBV 三角交叉验证）
        if vol["turn_rate"] > 0:
            if vol["turn_rate"] > 8:  # 换手率>8% 异常活跃
                if pos["pct_5d"] > 0:
                    score += 5
                    reasons_bull.append(f"高换手({vol['turn_rate']:.1f}%)放量上攻")
                else:
                    score -= 5
                    reasons_bear.append(f"高换手({vol['turn_rate']:.1f}%)主力可能出货")
            elif vol["turn_rate"] < 1 and vol["avg_turn_5d"] > 2:  # 换手率突降
                reasons_bear.append(f"换手萧条({vol['turn_rate']:.1f}%←均{vol['avg_turn_5d']:.1f}%)")

        # ── 布林带位置 (±10) ──
        if boll["boll_position"] > 80:
            score += 5
            reasons_bull.append("价格靠近布林上轨(强势)")
            if boll["boll_position"] > 95:
                score -= 3  # 过于接近上轨有回调风险
                reasons_bear.append("触及布林上轨(短期回调风险)")
        elif boll["boll_position"] < 20:
            score -= 5
            reasons_bear.append("价格靠近布林下轨(弱势)")
            if boll["boll_position"] < 5:
                score += 3  # 极端超卖可能反弹
                reasons_bull.append("触及布林下轨(超卖反弹)")

        # ── 价格位置 (±8) ──
        if pos["pct_5d"] > 5:
            score += 8
            reasons_bull.append(f"近5日强势上涨{pos['pct_5d']:+.1f}%")
        elif pos["pct_5d"] > 2:
            score += 4
        elif pos["pct_5d"] < -5:
            score -= 8
            reasons_bear.append(f"近5日大幅下跌{pos['pct_5d']:+.1f}%")
        elif pos["pct_5d"] < -2:
            score -= 4

        # ── K线形态加分 ──
        for p in patterns:
            if p == "放量突破MA20":
                score += 12
                reasons_bull.append("放量突破MA20!")
            elif p == "三连阳":
                score += 8
                reasons_bull.append("三连阳")
            elif p == "三连阴":
                score -= 12  # v3: -8→-12，回填84%胜率最强看空指标
                reasons_bear.append("三连阴")
            elif p == "长下影线(锤子)":
                score += 5
                reasons_bull.append("锤子线(底部信号)")
            elif p == "长上影线(射击之星)":
                score -= 5
                reasons_bear.append("射击之星(顶部信号)")
            elif p == "缩量回调" and ma.get("bullish_aligned"):
                score += 3
                reasons_bull.append("多头排列中缩量回调(洗盘)")

        # ── 映射到 signal (降低阈值) ──
        if score >= 25:
            signal = "看多"
            strength = "强"
        elif score >= 8:
            signal = "偏多"
            strength = "弱"
        elif score <= -25:
            signal = "看空"
            strength = "强"
        elif score <= -8:
            signal = "偏空"
            strength = "弱"
        else:
            signal = "中性"
            strength = "无"

        # 置信度: 基于多空理由数量和评分幅度
        n_reasons = len(reasons_bull) + len(reasons_bear)
        confidence = min(abs(score) + n_reasons * 3, 100)

        return {
            "score": score,
            "signal": signal,
            "strength": strength,
            "confidence": confidence,
            "reasons_bull": reasons_bull,
            "reasons_bear": reasons_bear,
        }
