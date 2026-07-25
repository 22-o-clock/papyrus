# Papyrus

Papyrus は、会話支援、モデレーション、リマインダー、音声読み上げなどを提供する Discord Bot です。
Python 3.13 以上と [uv](https://docs.astral.sh/uv/) を使用します。

## セットアップ

サブモジュールを取得し、`.env.sample` を参考に `.env` を作成してください。

```sh
git submodule update --init --recursive
uv sync
uv run main.py
```

## ドキュメント

- [ドキュメント一覧](docs/index.md)
- [機能概要](docs/features.md)
- [開発・保守ガイドライン](docs/development.md)
