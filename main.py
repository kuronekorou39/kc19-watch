"""エントリポイント: Webダッシュボード + Telegram Bot を起動する。

    uv run kemocon      # 推奨(pyproject の script 経由)
    python main.py      # 同等
"""
from kemocon.web import run

if __name__ == "__main__":
    run()
