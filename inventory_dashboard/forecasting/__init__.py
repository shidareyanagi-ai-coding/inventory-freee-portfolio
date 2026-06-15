"""需要予測レベル2（EVOLUTION_PLAN.md A-4）。

このパッケージが「合成データ生成・特徴量・モデル（baseline/SARIMA/LightGBM）・
バックテスト・予測の書き込み」を担う。データアクセス境界どおり、DB 読み書きは
サーバ側(Python)が唯一の主体であり、ここはその予測ドメインを実装する。

import 規約: cwd=inventory_dashboard 前提（app.py と同じ flat レイアウト）。
パッケージ内から `import db` が解決できる。CLI は `python -m forecasting.run`。
"""
