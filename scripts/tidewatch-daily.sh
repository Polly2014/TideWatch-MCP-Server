#!/bin/bash
# TideWatch daily cron — 每个工作日北京 17:00 (UTC 09:00)
# 1) scan_market 刷新持仓/自选价格
# 2) analyze_stock 对每只持仓生成信号（含 LLM 叙事润色）
# 3) update_signal_outcomes 回填历史信号

LOG=/var/log/tidewatch-daily.log
API_KEY=$(grep "^MCP_API_KEY=" /home/azureuser/GitHub_Workspace/TideWatch-MCP-Server/.env | cut -d= -f2)
URL=http://localhost:8889/mcp

# ── 交易日检测 — 非交易日(节假日/周末) early exit ──
# 用 baostock 查沪深300今天有没有 K 线，无数据 = 非交易日
# 如果 baostock 自身挂了（返回空），宁可空跑也不漏跑
TODAY_BJ=$(TZ=Asia/Shanghai date +%Y-%m-%d)
IS_TRADING=$(cd /home/azureuser/GitHub_Workspace/TideWatch-MCP-Server && poetry run python3 -c "
import baostock as bs
bs.login()
rs = bs.query_history_k_data_plus('sh.000300', 'date,close', start_date='$TODAY_BJ', end_date='$TODAY_BJ', frequency='d')
rows = []
while rs.error_code == '0' and rs.next():
    rows.append(rs.get_row_data())
bs.logout()
print('YES' if rows else 'NO')
" 2>/dev/null)

if [ -z "$IS_TRADING" ]; then
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Trading day check failed (baostock error?), running anyway" >> $LOG
elif [ "$IS_TRADING" = "NO" ]; then
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $TODAY_BJ is not a trading day, skipping" >> $LOG
    exit 0
fi

call_mcp() {
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Calling $1..." >> $LOG
    R=$(curl -s -m ${3:-120} -X POST $URL -H 'Content-Type: application/json' -H 'Accept: application/json' -H "Authorization: Bearer $API_KEY" -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/call\",\"params\":{\"name\":\"$1\",\"arguments\":$2}}")
    echo "$R" | python3 -c 'import json,sys; d=json.load(sys.stdin); sys.exit(0 if "result" in d else 1)' 2>/dev/null && echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $1 OK" >> $LOG || echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $1 FAILED" >> $LOG
}

echo '========================================' >> $LOG
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Daily scan started" >> $LOG

# Step 1: Refresh scan cache
call_mcp scan_market '{"tier":"holdings"}' 120
call_mcp scan_market '{"tier":"watchlist"}' 120

# Step 2: Analyze each holding with LLM (generates signals for tracking)
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Analyzing holdings with LLM..." >> $LOG
HOLDINGS_JSON=$(curl -s -m 30 -X POST $URL -H 'Content-Type: application/json' -H 'Accept: application/json' -H "Authorization: Bearer $API_KEY" -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"manage_holdings","arguments":{"action":"list"}}}')
SYMBOLS=$(echo "$HOLDINGS_JSON" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
    text = d.get("result", {}).get("content", [{}])[0].get("text", "")
    data = json.loads(text)
    holdings = data.get("holdings", [])
    for h in holdings:
        sym = h.get("symbol", "")
        if sym:
            print(sym)
except:
    pass
' 2>/dev/null)

if [ -n "$SYMBOLS" ]; then
    for sym in $SYMBOLS; do
        call_mcp analyze_stock "{\"symbol\":\"$sym\"}" 120
        sleep 2
    done
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Holdings analysis completed" >> $LOG
else
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] No holdings found, skipping" >> $LOG
fi

# Step 3: Backfill signal outcomes
call_mcp update_signal_outcomes '{}' 600

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Daily scan completed" >> $LOG
