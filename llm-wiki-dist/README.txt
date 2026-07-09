```bash
cd frontend

npm i

npm run build

cd ..

uv sync

WIKI_DB=.wiki/moove.sqlite uv run  uvicorn app:app --port 8000 --host 0.0.0.0
```
