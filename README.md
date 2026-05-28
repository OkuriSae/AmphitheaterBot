# AmphitheaterBot

Python と `discord.py` で作る Discord Bot です。

## セットアップ

1. Discord Developer Portal でアプリケーションを作成します。
2. `Bot` ページで bot を作成し、token をコピーします。
3. このプロジェクトで仮想環境を作り、依存関係を入れます。

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

4. `.env.example` を `.env` にコピーし、`DISCORD_TOKEN` に token を設定します。

```bash
cp .env.example .env
```

開発中は `DISCORD_GUILD_ID` に自分の Discord サーバー ID を入れると、Slash Command の反映が速くなります。

`/battle` の参加者 rating は、`RATINGS_CSV_URL` の Google Sheets CSV から読み込みます。
シートには `userid` と `rating` の列を用意してください。
未登録ユーザーは、Discord のユーザー名を `userid`、初期値 `1000` を `rating` として追加されます。
未登録ユーザーをシート末尾へ追加するには、Google Cloud のサービスアカウント JSON を作成し、
その `client_email` に対象スプレッドシートの編集権限を付与してから、`.env` の
`GOOGLE_SERVICE_ACCOUNT_FILE` に JSON ファイルのパスを設定してください。

## 起動

```bash
source .venv/bin/activate
python bot.py
```

## 招待 URL

Developer Portal の `OAuth2` -> `URL Generator` で、以下を選んで Bot をサーバーに招待します。

- Scopes: `bot`, `applications.commands`
- Bot Permissions: まずは権限なし、または必要な権限だけ

## コマンド

- `/battle`: Civilization6 のマルチプレイ卓を募集します。任意で募集タイトルを指定できます。参加者の rating を Google Sheets から表示します。
- `/swap`: 直前のチーム分けで、指定した2人のチームを入れ替えます。例: `/swap id: @okurisae @hakoeda`
