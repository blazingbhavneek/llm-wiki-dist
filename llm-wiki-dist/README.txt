```bash
cd frontend

npm i

npm run build

cd ..

uv sync

WIKI_DB=.wiki/moove.sqlite uv run  uvicorn app:app --port 8000 --host 0.0.0.0

# In another process; the database is selected per endpoint URL.
uv run python mcp_server.py --port 8001 --host 0.0.0.0

# Example MCP endpoint for .wiki/moove.sqlite:
# http://localhost:8001/llm-wiki/moove/mcp
```
