# README: `llm-wiki-rikiseisan` の実行

## 概要

このイメージには、`llm-wiki-rikiseisan:0.0.2` のWebアプリケーションとバックエンドサーバーが含まれています。

Dockerイメージは圧縮されたtarファイルとして配布されています。`scp` でコピーし、Dockerにロードしてからコンテナを起動します。

---

## イメージの場所

```text
Host: 10.160.152.38
Path: /home/seigyo/docker-images/v0.0.2/llm-wiki-rikiseisan:0.0.2.tar.gz
```

イメージをコピーしてロードします:

```bash
scp seigyo@10.160.152.38:/home/seigyo/docker-images/v0.0.2/llm-wiki-rikiseisan:0.0.2.tar.gz .
docker load -i llm-wiki-rikiseisan:0.0.2.tar.gz
```

イメージがロードされたことを確認します:

```bash
docker images | grep llm-wiki-rikiseisan
```

想定されるイメージ:

```text
llm-wiki-rikiseisan:0.0.2
```

---

## コンテナの起動

```bash
docker run -d --name llm-wiki-rikiseisan \
  -p 51025:8000 -p 51026:8001 -p 51024:22 \
  -e WIKI_PREFIX="/llm-wiki" \
  llm-wiki-rikiseisan:0.0.2
```

ポートマッピング:

| ホストポート | コンテナポート | 用途 |
|---:|---:|---|
| `51025` | `8000` | Webアプリ / バックエンド |
| `51026` | `8001` | MCP (`/llm-wiki/{db}/mcp`) |
| `51024` | `22` | コンテナへのSSHアクセス |

バックエンドサーバーはコンテナ内で自動的に起動します。

WebアプリURL:

```text
http://10.160.152.38:51025/llm-wiki
```

MCP URLはSQLite名をパスで選択します:

```text
http://10.160.152.38:51026/llm-wiki/wiki/mcp
http://10.160.152.38:51026/llm-wiki/moove/mcp
```

---

## SSHアクセス

マッピングされたSSHポートを通して、実行中のコンテナにSSH接続できます:

```bash
ssh -p 51024 seigyo@10.160.152.38
```

パスワード:

```text
seigyo@rikiseisan
```

コンテナ内では、バックエンドは `backend` という名前の `tmux` セッションで実行されています。

次のコマンドでアタッチします:

```bash
tmux attach -t backend -d
```

これは開発時に便利です。実行中のバックエンドを確認したり、停止したり、手動で再起動したり、コンテナ内でアプリケーションを修正・デバッグしたりできます。

---

## 任意: Wikiデータベースのバインドマウント

デフォルトでは、コンテナは内部の `.wiki` ディレクトリを使用します。ホスト側のSQLiteデータベースを使用または永続化したい場合は、ホストフォルダをマウントします:

```bash
docker run -d --name llm-wiki-rikiseisan \
  -p 51025:8000 -p 51026:8001 -p 51024:22 \
  -e WIKI_PREFIX="/llm-wiki" \
  -v "$PWD/.wiki_docker:/home/seigyo/llm-wiki/.wiki" \
  llm-wiki-rikiseisan:0.0.2
```

これは次のようにマッピングします:

```text
Host:      $PWD/.wiki
Container: /home/seigyo/llm-wiki/.wiki
```

例:

ホストフォルダに次が含まれている場合:

```text
$PWD/.wiki/moove.sqlite
```

その場合、アプリは次のURLで利用できます:

```text
http://10.160.152.38:51025/llm-wiki/moove
```

新しいwiki名を開いた場合、例えば:

```text
http://10.160.152.38:51025/llm-wiki/meetings
```

その場合、アプリは新しいデータベースを作成します:

```text
$PWD/.wiki/meetings.sqlite
```

初回作成時には、初期化が完了するまでの数秒間、ページにエラーが表示される場合があります。少し待ってから更新してください。

---

## 環境変数

| 変数 | 用途 |
|---|---|
| `WIKI_PREFIX="/llm-wiki"` | リバースプロキシ対応のためのURLプレフィックス。明示的に設定していない場合でも、デフォルトは `/llm-wiki` です。 |
| `WIKI_EMBED_BASE_URL` | 埋め込みサーバーのエンドポイント。既存のデータベースがすでに作成されている場合、通常は変更しないでください。 |
| `WIKI_RERANK_BASE_URL` | リランカーサーバーのエンドポイント。既存のデータベースがすでに作成されている場合、通常は変更しないでください。 |

任意の埋め込み/リランカー設定を含む例:

```bash
docker run -d --name llm-wiki-rikiseisan \
  -p 51025:8000 -p 51026:8001 -p 51024:22 \
  -e WIKI_PREFIX="/llm-wiki" \
  -e WIKI_EMBED_BASE_URL=http://your-embed-server:8081/v1 \
  -e WIKI_RERANK_BASE_URL=http://your-rerank-server:8082/v1 \
  -v "$PWD/.wiki:/home/seigyo/llm-wiki/.wiki" \
  llm-wiki-rikiseisan:latest
```

重要: 既存のSQLiteデータベースがこれらの新しいサービスを使用すべきであると分かっている場合を除き、`WIKI_EMBED_BASE_URL` または `WIKI_RERANK_BASE_URL` を変更しないでください。既存のデータベースには、現在/デフォルトの埋め込みサーバーによって生成された埋め込みがすでに含まれている可能性があります。

---

## 停止と削除

コンテナを停止します:

```bash
docker stop llm-wiki-rikiseisan
```

コンテナを削除します:

```bash
docker rm llm-wiki-rikiseisan
```

削除後、同じ名前で再起動します:

```bash
docker run -d --name llm-wiki-rikiseisan \
  -p 51025:8000 -p 51026:8001 -p 51024:22 \
  -e WIKI_PREFIX="/llm-wiki" \
  llm-wiki-rikiseisan:latest
```
