"""pytest 共通設定（A-6）。

テストでは seed 時の重い予測バッチ(run_forecast)を無効化する:
  - 速度（毎回の3モデル学習＋バックテストを避ける）
  - 既存テストは「簡易計算ベースの必要在庫」を前提にしているため挙動を維持する
AI 予測ベースの必要在庫を検証するテストは、各テストで run_forecast を明示的に実行する。
"""

import app

app.RUN_FORECAST_ON_SEED = False
