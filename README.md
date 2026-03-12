# 🌊 观潮 (TideWatch)

> AI 投研搭档 — 不是给你看数据，而是跟你聊投资。

MCP-Native 架构的多维融合股票分析引擎。在 Claude / Cursor / 任何 MCP 客户端中直接使用。

## Features

- 🧠 **多维融合分析** — 技术面 + 资金面 + 消息面 + 市场体制，四维交叉验证
- ⚡ **冲突检测** — "技术面看多但主力在出货"，矛盾信号自动预警
- 🌊 **市场体制感知** — 牛市/熊市/横盘/高波动，同一形态不同解读
- 🔌 **MCP-Native** — 即插即用，在 AI 对话中直接调用

## Quick Start

```bash
# 本地模式 (stdio)
poetry install
poetry run tidewatch
```

## Remote Deployment

TideWatch 支持远程 HTTP 部署，通过 Nginx + SSL 反向代理对外提供服务。

```bash
# Azure VM 上
git clone https://github.com/Polly2014/TideWatch-MCP-Server.git
cd TideWatch-MCP-Server
./setup.sh                        # 安装依赖 + 自动生成 API Key
nano .env                         # 设置 COPILOTX_API_KEY
sudo ./scripts/setup_domain.sh    # 配置 Nginx + SSL
sudo systemctl enable --now tidewatch
```

客户端配置（VS Code `mcp.json` / Cursor `.cursor/mcp.json`）：

```json
{
    "TideWatch": {
        "url": "https://tidewatch.polly.wang/mcp",
        "headers": { "X-API-Key": "polly-tidewatch-xxx" }
    }
}
```

## License

MIT
