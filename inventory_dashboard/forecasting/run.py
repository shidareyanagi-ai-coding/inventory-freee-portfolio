"""予測バッチの CLI（EVOLUTION_PLAN.md A-4）。

使い方（cwd=inventory_dashboard）:
  python -m forecasting.run            # 全組織を再計算
  python -m forecasting.run --org 1    # 指定組織のみ

DATABASE_URL 未設定なら app.DB_PATH の SQLite、postgres:// なら Neon 等。
"""

from __future__ import annotations

import argparse

import app
from forecasting import service


def main() -> None:
    parser = argparse.ArgumentParser(description="需要予測バッチ（A-4）")
    parser.add_argument("--org", type=int, default=None, help="対象 organization_id（省略時は全組織）")
    parser.add_argument("--horizon", type=int, default=30, help="予測日数")
    args = parser.parse_args()

    app.init_db()
    with app.get_conn() as conn:
        if args.org is not None:
            org_ids = [args.org]
        else:
            org_ids = [
                row["id"]
                for row in conn.execute("SELECT id FROM organizations ORDER BY id").fetchall()
            ]
        if not org_ids:
            print("組織がありません（まずログイン/seed してください）。")
            return
        for organization_id in org_ids:
            summary = service.run_forecast(
                conn, organization_id, horizon_days=args.horizon, actor_user_id="cli"
            )
            print(f"org {organization_id}: {summary}")


if __name__ == "__main__":
    main()
