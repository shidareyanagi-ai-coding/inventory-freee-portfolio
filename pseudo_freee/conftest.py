"""pytest 共通設定（A-8）。

テストは**常にローカル保存**で動かす（.env に実 R2 の STORAGE_* があっても実 R2 に書かない）。
app.py 取り込み時の load_dotenv() が .env の STORAGE_* を読み込むため、ここで先に「空」に
固定し object_storage_enabled() を False にする（load_dotenv は override=False ＝既存キーを
上書きしないので、空のまま保たれる）。storage の S3/R2 経路自体は test_storage.py が boto3 を
モックして検証する（在庫 test_invoice_capture.py の「テストは常にローカル保存」と同じ安全策）。
"""

import os

for _key in (
    "STORAGE_ENDPOINT",
    "STORAGE_REGION",
    "STORAGE_BUCKET",
    "STORAGE_ACCESS_KEY_ID",
    "STORAGE_SECRET_ACCESS_KEY",
):
    os.environ[_key] = ""
