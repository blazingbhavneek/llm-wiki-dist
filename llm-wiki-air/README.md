# Growi LLM Wiki — 運用ガイド

文書が入ったフォルダ（NFS やバインドマウント）を受け取り、AI が書き直した
相互リンク付き Markdown ウィキを **GROWI** のプロジェクト名付きフォルダ 1 つに公開します。
閲覧や編集は GROWI 上で行い、生成したページは本ツールが管理します。

操作はすべて `main.py` の 1 つのプログラムから行い、プロジェクト 1 件 = `configs/` の 1 ファイルです。
設計や内部仕様など開発者向けの内容は [`DEV_README.md`](DEV_README.md) を参照してください。

## 出力先

| 場所                | 内容                                                             |
| ------------------- | ---------------------------------------------------------------- |
| ソース（マウント元） | 元の文書そのもの。変更も複製もしません                           |
| `data/<name>/`      | ローカルの作業データ（変換後のテキスト、生成ページ、進捗状態）   |
| GROWI `/<name>/`    | 公開されたウィキ（番号付きページ、文書間リンク、`00-目次` 一覧） |

## 対応ファイル形式

`.md` `.txt` `.pdf` `.docx` `.doc` `.pptx` `.xlsx` `.xlsm` `.xls` `.csv`

- `.md` / `.txt` はそのまま使用します（変換サービスは不要）。
- それ以外は外部の **doc-parser** サービス（GPU ホストで稼働）で変換します。
- `.xlsm` は静的に解析します。マクロは実行せず、ワークシートと VBA はそれぞれ別ページとして公開します。
- Word/Excel 97-2003（`.doc`、`.xls`）はあらかじめ変換します。`.xls` のマクロは保持されません。

## 初回のみ：環境セットアップ

```bash
uv sync     # .venv/ を作成
```

リポジトリ直下の `.env` を編集します。主要な項目は以下のとおりです。

| キー                                        | 内容                                          |
| ------------------------------------------- | --------------------------------------------- |
| `WIKI_CHAT_BASE_URL`（または `OPENAI_BASE_URL`） | 主要 LLM のエンドポイント（OpenAI 互換）   |
| `WIKI_CHAT_API_KEY`（または `OPENAI_API_KEY`）   | その API キー。不要なエンドポイントでは `local` |
| `WIKI_MODEL`（または `WIKI_CHAT_MODEL`）         | エンドポイント上のモデル名                    |
| `WIKI_PARSER_BASE_URL`                      | doc-parser サービスの URL（`.md` 以外に必要） |
| `GROWI_URL`、`GROWI_TOKEN`                  | GROWI のアドレスと API トークン               |
| `WIKI_DATA_ROOT`                            | ローカル作業データの場所（既定 `./data`）     |
| `WIKI_CONCURRENCY`                          | LLM 呼び出しの並列数（既定 `4`）              |
| `WIKI_OUTPUT_LANGUAGE`                      | 生成ページの言語（既定は日本語）              |

設定は次の順で読み込み、後ろほど優先度が高くなります。
**組み込みデフォルト → `.env` → `configs/<project>.ini` → コマンドライン引数**
`.env` はマシン共通の設定、プロジェクトファイルはそのプロジェクト分のみ上書きします。

初回実行前に、設定と接続を確認します。

```bash
.venv/bin/python main.py check --project projectA
```

## 新しいソースフォルダを追加する

1. **マウント**します（絶対パスで読み取り可能であること。`--bind`、NFS、SMB のいずれでも可）:

   ```bash
   sudo mkdir -p /home/seigyo/mnt/newproj
   sudo mount --bind /path/to/documents /home/seigyo/mnt/newproj
   ```
2. **プロジェクトファイル** `configs/newproj.ini` を作成します。ファイル名がそのまま `--project` の指定名になります:

   ```ini
   [project]
   source_mount = /home/seigyo/mnt/newproj   # マウントした文書フォルダへの絶対パス
   target_name  = NewProj                    # フォルダ名 1 つ: data/NewProj/ と GROWI の /NewProj
   data_root    = data                       # ローカル作業データ（絶対パス、またはこのリポジトリ基準の相対パス）

   # 任意: このプロジェクトだけ別の GROWI を使う場合
   growi_url   = http://growi.example:3000/
   growi_token = API Token:<token>
   ```
3. **確認して実行**します:

   ```bash
   .venv/bin/python main.py check --project newproj
   .venv/bin/python main.py -v sync --project newproj
   ```

## プロジェクトファイルで変更できる設定

`[project]` には上記の 3 つの必須キーを置き、任意の `[settings]` 節で実行時設定を
プロジェクト単位で上書きできます。よく使う項目:

| キー                                                | 効果                                          |
| --------------------------------------------------- | --------------------------------------------- |
| `chat_base_url`、`chat_model`、`chat_api_key`      | このプロジェクトで使う LLM のエンドポイントとモデル |
| `parser_base_url`、`parser_timeout`                | doc-parser サービスの URL と、大容量ファイルの待ち時間上限 |
| `growi_url`、`growi_token`、`growi_mode`           | GROWI の設定。`attach` はプロジェクトフォルダ外に書き込みません |
| `concurrency`                                     | LLM 呼び出しの並列数                          |
| `wiki_linker_enabled`、`wiki_linker_mode`          | 文書間リンクの on/off と、使うリンカーの種類  |
| `wiki_output_language`                            | 生成ページの言語                              |
| `data_root`（`[project]`）または `--data-root`   | 作業データを別のディスクへ移動                |

存在しないキー名は起動時にエラーになります（打ち間違いが無視されず通知されます）。

## 日常の使い方

| 目的                                 | コマンド                                                       |
| ------------------------------------ | -------------------------------------------------------------- |
| パイプライン全体を一度実行           | `main.py -v sync --project projectA`                         |
| 特定の文書だけを再実行               | `main.py -v sync --project projectA --force "path/file.pdf"` |
| 生成済みページをそのまま公開         | `main.py publish --project projectA`                         |
| 公開せずローカルだけ再生成           | `main.py -v build --project projectA all`                    |
| リンク処理だけ再実行                 | `main.py -v build --project projectA link`                   |
| GROWI 側の編集を取り込む（生成なし） | `main.py pull --project projectA`                            |
| このプロジェクトの GROWI ページを削除 | `main.py reset --project projectA`                           |

`sync` は変換・生成・リンク・公開までを一括で実行し、失敗した処理を 1 回再試行します。
生成に失敗した文書は公開されません。
`publish` は生成済みのページを送信します（リンクが未完了でも公開されます）。
先にリンクを完了させ、リンク完了済みの文書だけを公開する場合は `--no-allow-unlinked` を付けます。

すべてのコマンドはリポジトリ直下で `.venv/bin/python main.py …` の形式で実行します。

## 大容量のフォルダ: `--fast`

通常の処理では時間がかかりすぎる大容量のマウント向けです。

```bash
.venv/bin/python main.py -v sync --project projectA --fast
```

`--fast` は生成処理を簡略化し、**文書間のリンク処理を省略**します。つまりページは
文書間リンクなしで公開されます。あとからリンクを付けて再公開する場合:

```bash
.venv/bin/python main.py -v build --project projectA link
.venv/bin/python main.py publish --project projectA
```

## フォルダを常に最新に保つ

`watch` は起動し続けるプログラムです。`--interval` 秒ごと（既定 `86400`＝1 日 1 回）に
マウントを走査して新規・変更ファイルを確認し、5 分ごとに GROWI 側の編集有無を照会します。
バックグラウンドで起動したままにします。同一プロジェクトで多重起動はできません
（先のインスタンスが生きている間は起動を拒否します）。

```bash
# ターミナルを閉じても継続
nohup .venv/bin/python main.py -v watch --project projectA >> data/<name>/metadata/watch.log 2>&1 &

# 後から接続できるターミナルセッションで動かす（tmux は $HOME で開始するため cd を付ける）
tmux new -s wiki 'cd /path/to/this/repo && .venv/bin/python main.py -v watch --project projectA'
```

停止は tmux 内で Ctrl-C、または `pkill -f "watch --project projectA"`。
一度だけ更新して終了したい場合（マウントにファイルを置いた直後や、既存の定期実行）は
`sync` を実行します。
