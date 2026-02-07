# UCP Shopping Agent

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Docker](https://img.shields.io/badge/Docker-ready-2496ED.svg?logo=docker)](Dockerfile)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-009688.svg?logo=fastapi)](https://fastapi.tiangolo.com)
[![LangGraph](https://img.shields.io/badge/LangGraph-agent-4B0082.svg)](https://github.com/langchain-ai/langgraph)
[![UCP](https://img.shields.io/badge/UCP-2026--01--11-FF6F00.svg)](https://ucp.dev)

**AI Shopping Agent** that discovers UCP merchants, compares prices across vendors, optimizes multi-merchant orders, and orchestrates autonomous purchases -- like what powers Google AI Mode in Search and Gemini.

> **Buyer-side UCP implementation.** While the [UCP Merchant Server](https://github.com/samuelvinay91/ucp-merchant-server) shows how merchants serve the protocol (like Shopify), this project shows how AI agents **consume** it to shop across multiple stores.

---

## What This Project Demonstrates

| Concept | Implementation |
|---------|---------------|
| **UCP Discovery** | Fetch `/.well-known/ucp` from multiple merchants in parallel |
| **Multi-Merchant Search** | Fan-out product search across all discovered merchants |
| **Price Comparison** | Build comparison matrix with price, shipping, and ratings |
| **Split-Order Optimization** | Buy items from cheapest vendors to minimize total cost |
| **LangGraph Workflow** | 9-node state graph: plan -> discover -> search -> compare -> optimize -> confirm -> checkout -> complete |
| **Human-in-the-Loop** | Confirmation step before any purchase is executed |
| **SSE Streaming** | Real-time shopping progress events |
| **MCP Tool Surface** | All operations exposed as MCP tools |
| **3 Mock Merchants** | Built-in TechZone, HomeGoods, MegaMart for instant demo |

---

## Quick Start

### Docker Compose (Recommended)

```bash
git clone https://github.com/samuelvinay91/ucp-shopping-agent.git
cd ucp-shopping-agent

cp .env.example .env
# Edit .env with your API keys (optional - works without LLM for basic flows)

docker compose up --build
```

The API will be available at **http://localhost:8020**. Docs at **http://localhost:8020/docs**.

### Local Development

```bash
git clone https://github.com/samuelvinay91/ucp-shopping-agent.git
cd ucp-shopping-agent

python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

python -m ucp_shopping.main
```

---

## Shopping Workflow

```
User: "Find me the best deal on a mechanical keyboard and a USB-C hub"

    [PLAN] Parse intent -> extract items + constraints
      |
    [DISCOVER] Fetch /.well-known/ucp from 3 merchants
      |
    [SEARCH] Fan-out parallel search across all merchants
      |
    [COMPARE] Build price/shipping comparison matrix
      |                          +---------------------------+
      |                          | Keyboard    | USB-C Hub   |
      |                          |-------------|-------------|
      |                          | TechZone    | $79 + $5.99 |
      |                          | HomeGoods   | $89 + FREE  |
      |                          | MegaMart    | $69 + $8.99 |
      |                          +---------------------------+
      |
    [OPTIMIZE] Split-order: keyboard from MegaMart ($69), hub from HomeGoods ($34)
      |
    [PRESENT] Show comparison + recommendation via SSE
      |
    [CONFIRM] Wait for human approval
      |
    [CHECKOUT] Create sessions at MegaMart + HomeGoods simultaneously
      |
    [COMPLETE] Aggregate orders -> unified tracking
```

---

## API Reference

### Shopping (Main Flow)

```bash
# Start a shopping session
curl -X POST http://localhost:8020/api/v1/shop \
  -H "Content-Type: application/json" \
  -d '{"query": "Find me a mechanical keyboard under $100"}'

# Check shopping progress
curl http://localhost:8020/api/v1/shop/{session_id}

# Stream real-time progress (SSE)
curl -N http://localhost:8020/api/v1/shop/{session_id}/stream

# Confirm purchase
curl -X POST http://localhost:8020/api/v1/shop/{session_id}/confirm

# Cancel shopping
curl -X POST http://localhost:8020/api/v1/shop/{session_id}/cancel
```

### Price Comparison

```bash
# Compare a product across all merchants
curl -X POST http://localhost:8020/api/v1/compare \
  -H "Content-Type: application/json" \
  -d '{"query": "gaming mouse"}'

# Optimize a multi-item order
curl -X POST http://localhost:8020/api/v1/optimize \
  -H "Content-Type: application/json" \
  -d '{"items": ["mechanical keyboard", "USB-C hub", "monitor stand"]}'
```

### Merchant Discovery

```bash
# List known merchants
curl http://localhost:8020/api/v1/merchants

# Discover a new merchant
curl -X POST http://localhost:8020/api/v1/merchants/discover \
  -d '{"urls": ["https://merchant.example.com"]}'

# Browse a merchant's catalog
curl http://localhost:8020/api/v1/merchants/techzone/catalog
```

### Order Tracking

```bash
# List all orders across merchants
curl http://localhost:8020/api/v1/orders

# Get specific order
curl http://localhost:8020/api/v1/orders/{order_id}
```

---

## Built-in Mock Merchants

Three UCP-compliant mock merchants are included for instant demo:

| Merchant | Products | Specialty | Port Path |
|----------|----------|-----------|-----------|
| **TechZone** | 20 electronics | Laptops, keyboards, mice | `/merchants/techzone` |
| **HomeGoods** | 20 home/office | Desk accessories, lighting | `/merchants/homegoods` |
| **MegaMart** | 20 general | Overlapping catalog, different prices | `/merchants/megamart` |

Each merchant serves `/.well-known/ucp`, product catalog, checkout sessions, and orders.

---

## SSE Events

When streaming a shopping session (`GET /api/v1/shop/{id}/stream`), you'll receive these events:

```
event: planning
data: {"message": "Parsing shopping request...", "items": ["keyboard", "hub"]}

event: merchants_discovered
data: {"count": 3, "merchants": ["TechZone", "HomeGoods", "MegaMart"]}

event: searching
data: {"merchant": "TechZone", "query": "keyboard"}

event: products_found
data: {"merchant": "TechZone", "count": 5}

event: comparison_ready
data: {"matrix": [...], "best_price": {...}}

event: optimization_ready
data: {"plan": {...}, "savings": "$12.50"}

event: awaiting_confirmation
data: {"total": "$103.99", "merchants": 2}

event: checkout_progress
data: {"merchant": "MegaMart", "status": "completed"}

event: completed
data: {"orders": [...], "total": "$103.99"}
```

---

## Testing

```bash
pytest tests/ -v
pytest tests/ -v --cov=src/ucp_shopping
pytest tests/ -v -m "not slow and not integration"
```

---

## Project Structure

```
ucp-shopping-agent/
├── src/ucp_shopping/
│   ├── main.py                    # Entry point + mount mock merchants
│   ├── api.py                     # FastAPI routes
│   ├── config.py                  # Settings
│   ├── models.py                  # All Pydantic models
│   ├── streaming.py               # SSE event stream
│   ├── orchestrator/
│   │   ├── graph.py               # LangGraph shopping workflow
│   │   ├── state.py               # Graph state schema
│   │   └── planner.py             # LLM-powered intent parser
│   ├── protocols/
│   │   ├── ucp_client.py          # UCP protocol client
│   │   ├── a2a_bridge.py          # A2A agent integration
│   │   └── mcp_surface.py         # MCP tool definitions
│   ├── agents/
│   │   ├── discovery_agent.py     # Merchant discovery
│   │   ├── search_agent.py        # Multi-merchant search
│   │   ├── comparison_agent.py    # Price comparison matrix
│   │   ├── optimizer.py           # Split-order optimization
│   │   └── checkout_agent.py      # Multi-merchant checkout
│   └── mock_merchants/
│       ├── merchant_factory.py    # Create mock UCP merchants
│       ├── merchant_app.py        # Reusable merchant mini-app
│       └── catalogs/              # Product data (JSON)
│           ├── techzone.json
│           ├── homegoods.json
│           └── megamart.json
├── tests/
├── k8s/
├── Dockerfile
├── docker-compose.yml
└── pyproject.toml
```

---

## Key Technologies

- **[UCP](https://ucp.dev)** - Universal Commerce Protocol (agent-side client)
- **[LangGraph](https://github.com/langchain-ai/langgraph)** - Multi-step shopping workflow
- **[A2A](https://github.com/google/A2A)** - Agent-to-Agent protocol bridge
- **[MCP](https://modelcontextprotocol.io)** - Model Context Protocol tool surface
- **[FastAPI](https://fastapi.tiangolo.com)** - API framework + SSE streaming

---

## License

MIT License - see [LICENSE](LICENSE) for details.
