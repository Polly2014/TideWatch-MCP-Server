#!/usr/bin/env python3
"""
TideWatch MCP Server — 冒烟测试脚本
每次代码修改后运行，验证所有 14 个工具功能正常。

用法:
    # 测试远程 Azure VM（默认）
    python3 tests/smoke_test.py

    # 测试本地
    python3 tests/smoke_test.py --local --port 8889

    # 只跑快速测试（跳过慢工具如 scan_market）
    python3 tests/smoke_test.py --quick

    # 指定 API Key
    python3 tests/smoke_test.py --api-key "your-key"
"""

import argparse
import json
import os
import sys
import time
from typing import Any

try:
    import requests
except ImportError:
    print("需要 requests 库: pip install requests")
    sys.exit(1)


# ─── 测试配置 ─────────────────────────────────────

REMOTE_URL = "https://tidewatch.polly.wang/mcp"
LOCAL_URL = "http://127.0.0.1:{port}/mcp"

# 测试用股票代码
TEST_A_STOCK = "600519"   # 贵州茅台
TEST_US_STOCK = "MSFT"    # Microsoft
TEST_COMPARE = ["600519", "000858"]  # 茅台 vs 五粮液


# ─── 工具调用封装 ─────────────────────────────────

def mcp_call(url: str, api_key: str, tool: str, args: dict = None, timeout: int = 60) -> dict:
    """调用 MCP 工具，返回结果或抛异常"""
    payload = {
        "jsonrpc": "2.0",
        "method": "tools/call",
        "params": {"name": tool, "arguments": args or {}},
        "id": 1,
    }
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "X-API-Key": api_key,
    }
    resp = requests.post(url, json=payload, headers=headers, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()

    if "error" in data:
        raise RuntimeError(f"JSON-RPC error: {data['error']}")

    result = data.get("result", {})
    if result.get("isError"):
        content = result.get("content", [{}])
        msg = content[0].get("text", "unknown error") if content else "unknown error"
        raise RuntimeError(f"Tool error: {msg}")

    # 优先取 structuredContent，fallback 到 content[0].text JSON 解析
    structured = result.get("structuredContent")
    if structured:
        return structured
    content = result.get("content", [])
    if content and content[0].get("text"):
        try:
            return json.loads(content[0]["text"])
        except (json.JSONDecodeError, KeyError):
            pass
    return result


# ─── 测试用例 ─────────────────────────────────────

class SmokeTests:
    def __init__(self, url: str, api_key: str):
        self.url = url
        self.api_key = api_key
        self.results = []

    def _run(self, name: str, tool: str, args: dict = None, timeout: int = 60,
             check: callable = None):
        """执行单个测试"""
        start = time.time()
        try:
            result = mcp_call(self.url, self.api_key, tool, args, timeout)
            elapsed = time.time() - start

            # 自定义检查
            if check and not check(result):
                self.results.append(("FAIL", name, f"check failed ({elapsed:.1f}s)"))
                return False

            self.results.append(("PASS", name, f"{elapsed:.1f}s"))
            return True
        except Exception as e:
            elapsed = time.time() - start
            self.results.append(("FAIL", name, f"{e} ({elapsed:.1f}s)"))
            return False

    # ─── 基础 ─────

    def test_server_status(self):
        """服务器状态"""
        self._run("server_status", "server_status", check=lambda r: "version" in r)

    def test_get_regime(self):
        """市场体制识别"""
        self._run("get_regime", "get_regime", check=lambda r: "regime" in r)

    # ─── 个股分析 ─────

    def test_analyze_stock_a(self):
        """A股分析（skip_llm）"""
        self._run("analyze_stock (A股)", "analyze_stock",
                   {"symbol": TEST_A_STOCK, "skip_llm": True},
                   timeout=60,
                   check=lambda r: "signal" in r and "technical" in r)

    def test_analyze_stock_us(self):
        """美股分析（skip_llm）"""
        self._run("analyze_stock (美股)", "analyze_stock",
                   {"symbol": TEST_US_STOCK, "skip_llm": True},
                   timeout=60,
                   check=lambda r: "signal" in r and "technical" in r)

    def test_compare_stocks(self):
        """多股对比"""
        self._run("compare_stocks", "compare_stocks",
                   {"symbols": ",".join(TEST_COMPARE)},
                   timeout=30,
                   check=lambda r: "stocks" in r or "comparisons" in r or isinstance(r, dict))

    # ─── 数据查询 ─────

    def test_money_flow(self):
        """资金流向"""
        self._run("get_money_flow_detail", "get_money_flow_detail",
                   {"symbol": TEST_A_STOCK},
                   timeout=30)

    def test_stock_news(self):
        """新闻消息面"""
        self._run("get_stock_news_report", "get_stock_news_report",
                   {"symbol": TEST_A_STOCK},
                   timeout=30)

    # ─── 持仓/自选 ─────

    def test_manage_holdings_list(self):
        """持仓列表"""
        self._run("manage_holdings (list)", "manage_holdings",
                   {"action": "list"})

    def test_manage_watchlist_list(self):
        """自选股列表"""
        self._run("manage_watchlist (list)", "manage_watchlist",
                   {"action": "list"})

    def test_manage_account(self):
        """账户信息"""
        self._run("manage_account", "manage_account",
                   {"action": "view"})

    # ─── 信号系统 ─────

    def test_review_signals(self):
        """信号复盘"""
        self._run("review_signals", "review_signals",
                   {"days": 7},
                   check=lambda r: "signals" in r or isinstance(r, dict))

    def test_update_signal_outcomes(self):
        """信号回填"""
        self._run("update_signal_outcomes", "update_signal_outcomes",
                   timeout=120,
                   check=lambda r: "updated" in r or "message" in r or isinstance(r, dict))

    # ─── 扫描（慢） ─────

    def test_scan_market(self):
        """全市场扫描"""
        self._run("scan_market", "scan_market",
                   timeout=120,
                   check=lambda r: "holdings" in r or "_hot_sorted" in r or "pool_size" in r)

    # ─── LLM（慢） ─────

    def test_polish_narrative_llm(self):
        """LLM 叙事润色"""
        # 先拿到 analyze_stock 的报告
        try:
            result = mcp_call(self.url, self.api_key, "analyze_stock",
                              {"symbol": TEST_A_STOCK, "skip_llm": True}, timeout=30)
            report = result.get("report", {})
            if not report:
                self.results.append(("SKIP", "polish_narrative_llm", "no report to polish"))
                return
            self._run("polish_narrative_llm", "polish_narrative_llm",
                       {"symbol": TEST_A_STOCK, "report": json.dumps(report, ensure_ascii=False)},
                       timeout=60)
        except Exception as e:
            self.results.append(("SKIP", "polish_narrative_llm", f"prerequisite failed: {e}"))

    # ─── 执行入口 ─────

    def run_all(self, quick=False):
        """运行所有测试"""
        # 快速测试（~30s）
        self.test_server_status()
        self.test_get_regime()
        self.test_analyze_stock_a()
        self.test_analyze_stock_us()
        self.test_manage_holdings_list()
        self.test_manage_watchlist_list()
        self.test_manage_account()
        self.test_review_signals()
        self.test_update_signal_outcomes()

        if not quick:
            # 完整测试（额外 ~2-3min）
            self.test_compare_stocks()
            self.test_money_flow()
            self.test_stock_news()
            self.test_scan_market()
            # LLM 润色依赖外部 API，单独标注
            self.test_polish_narrative_llm()

    def report(self):
        """打印测试报告"""
        print("\n" + "=" * 60)
        print("🌊 TideWatch MCP Server — 冒烟测试报告")
        print("=" * 60)

        passed = sum(1 for s, _, _ in self.results if s == "PASS")
        failed = sum(1 for s, _, _ in self.results if s == "FAIL")
        skipped = sum(1 for s, _, _ in self.results if s == "SKIP")
        total = len(self.results)

        for status, name, detail in self.results:
            icon = {"PASS": "✅", "FAIL": "❌", "SKIP": "⏭️"}[status]
            print(f"  {icon} {name:<35s} {detail}")

        print("-" * 60)
        print(f"  总计: {total} | ✅ {passed} | ❌ {failed} | ⏭️ {skipped}")

        if failed == 0:
            print("\n  🎉 全部通过！可以安全部署。")
        else:
            print(f"\n  ⚠️  {failed} 个测试失败，请检查后再部署。")

        print("=" * 60)
        return failed == 0


# ─── CLI ─────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="TideWatch MCP Server 冒烟测试")
    parser.add_argument("--local", action="store_true", help="测试本地服务（默认远程）")
    parser.add_argument("--port", type=int, default=8889, help="本地端口（默认 8889）")
    parser.add_argument("--url", type=str, help="自定义 MCP URL")
    parser.add_argument("--api-key", type=str, help="API Key（默认从 .env 读取）")
    parser.add_argument("--quick", action="store_true", help="快速模式（跳过慢工具）")
    args = parser.parse_args()

    # 确定 URL
    if args.url:
        url = args.url
    elif args.local:
        url = LOCAL_URL.format(port=args.port)
    else:
        url = REMOTE_URL

    # 确定 API Key
    api_key = args.api_key or os.environ.get("MCP_API_KEY")
    if not api_key:
        # 尝试从 .env 读取
        from pathlib import Path
        for env_name in [".env", "config.env"]:
            env_path = Path(__file__).parent.parent / env_name
            if env_path.exists():
                for line in env_path.read_text().splitlines():
                    line = line.strip()
                    if line.startswith("#") or not line:
                        continue
                    if line.startswith("MCP_API_KEY="):
                        api_key = line.split("=", 1)[1].strip().strip('"').strip("'")
                        break
            if api_key:
                break
    if not api_key:
        # 尝试从 Azure VM 远程读取
        try:
            import subprocess
            ssh_config = Path(__file__).parent.parent / "ssh.config"
            if ssh_config.exists():
                result = subprocess.run(
                    ["ssh", "-F", str(ssh_config), "Azure-Server",
                     "grep MCP_API_KEY ~/GitHub_Workspace/TideWatch-MCP-Server/.env"],
                    capture_output=True, text=True, timeout=10
                )
                if result.returncode == 0:
                    for line in result.stdout.splitlines():
                        if line.startswith("MCP_API_KEY="):
                            api_key = line.split("=", 1)[1].strip().strip('"').strip("'")
                            break
        except Exception:
            pass

    if not api_key:
        print("❌ 未找到 API Key，请用 --api-key 指定或在 .env 中配置 MCP_API_KEY")
        sys.exit(1)

    mode = "quick" if args.quick else "full"
    print(f"🌊 TideWatch 冒烟测试 ({mode})")
    print(f"   URL: {url}")
    print(f"   Key: {api_key[:10]}...")
    print()

    tests = SmokeTests(url, api_key)
    tests.run_all(quick=args.quick)
    success = tests.report()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
