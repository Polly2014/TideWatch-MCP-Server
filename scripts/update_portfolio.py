#!/usr/bin/env python3
"""更新持仓和账户信息"""
import requests, json

ENV_PATH = ".env"
URL = "https://tidewatch.polly.wang/mcp"

with open(ENV_PATH) as f:
    for line in f:
        if line.startswith("MCP_API_KEY="):
            key = line.split("=", 1)[1].strip()

def mcp(tool, args=None):
    r = requests.post(URL,
        json={"jsonrpc": "2.0", "method": "tools/call",
              "params": {"name": tool, "arguments": args or {}}, "id": 1},
        headers={"Content-Type": "application/json",
                 "Accept": "application/json, text/event-stream",
                 "X-API-Key": key},
        timeout=30)
    data = r.json()
    txt = data["result"]["content"][0]["text"]
    try:
        return json.loads(txt)
    except json.JSONDecodeError:
        return {"message": txt}

# 1. 威海广泰加仓: 4600→5100股, 成本→8.7845
print("1. 更新威海广泰 5100股 @8.7845...")
r = mcp("manage_holdings", {"action": "add", "symbol": "002111", "cost": 8.7845, "shares": 5100})
print(f"   {r.get('message', r)}")

# 2. 账户资金
print("2. 更新账户...")
r = mcp("manage_account", {"action": "update", "cash": 7926.89, "total_assets": 69222.89, "market_value": 61296.00})
print(f"   {r.get('message', r)}")

# 3. 验证
print("\n=== 验证 ===")
h = mcp("manage_holdings", {"action": "list"})
for s in h.get("holdings", []):
    print(f"  {s['symbol']} {s['name']} {s['shares']}股 @{s['cost']}")
a = mcp("manage_account", {"action": "view"})
acct = a.get("account", {})
print(f"  可用: {acct.get('cash')}, 总资产: {acct.get('total_assets')}, 市值: {acct.get('market_value')}")
