# 入口（ランチャー）ページ。GET /launcher で配信する（A-6: 統一ログイン＋アプリ選択）。
#
# 役割: 在庫ダッシュボードと疑似freee を「同じ Clerk ログイン」で選ぶ入口。
#   - サインイン後に2枚のカード（在庫 / 疑似freee）から選ぶ。
#   - 在庫アプリと同じ公開キーを使うので、同じアカウントで両アプリを行き来できる。
#   - 疑似freee は別サービス(別URL)。card のリンク先はサーバが env(PSEUDO_FREEE_API_URL)から渡す。
#
# ゲートの作りは index_html.py と同じ（公開キーから frontend-api を復号→ClerkJS→未サインインは伏せる）。
# 公開キー(pk_...)はブラウザに出して良い。秘密キーは絶対に埋め込まない。

import html
import json

_LAUNCHER_TEMPLATE = r"""<!doctype html>
<html lang="ja">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>業務アプリ入口</title>
  <style>
    :root { color-scheme: light; --bg: #eef2f7; --surface: #fff; --line: #d8dde5;
      --text: #20242a; --muted: #68717d; --accent: #256c64; --accent2: #2563eb; }
    * { box-sizing: border-box; }
    body { margin: 0; font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--bg); color: var(--text); }
    header { padding: 18px 24px; background: #24313d; color: #fff; display: flex;
      align-items: center; justify-content: space-between; gap: 16px; }
    header h1 { margin: 0; font-size: 19px; }
    #launcher-user { display: inline-flex; align-items: center; }
    main { max-width: 880px; margin: 0 auto; padding: 40px 20px; }
    .lead { color: var(--muted); margin: 0 0 26px; font-size: 14px; }
    .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 18px; }
    .app-card { display: flex; flex-direction: column; gap: 10px; background: var(--surface);
      border: 1px solid var(--line); border-radius: 12px; padding: 22px; text-decoration: none;
      color: inherit; box-shadow: 0 1px 2px rgba(32,36,42,.06); transition: transform .08s, box-shadow .08s; }
    .app-card:hover { transform: translateY(-2px); box-shadow: 0 10px 26px rgba(32,36,42,.14); text-decoration: none; }
    .app-card .icon { font-size: 30px; }
    .app-card h2 { margin: 0; font-size: 18px; }
    .app-card p { margin: 0; color: var(--muted); font-size: 13px; line-height: 1.6; }
    .app-card .go { margin-top: 6px; font-weight: 600; color: var(--accent2); font-size: 14px; }
    .app-card.inventory { border-top: 4px solid var(--accent); }
    .app-card.freee { border-top: 4px solid var(--accent2); }
    .app-card.disabled { opacity: .6; pointer-events: none; }
    .app-card.disabled .go { color: var(--muted); }
    /* Clerk サインインゲート */
    html.launcher-gated > body > header .user-actions,
    html.launcher-gated > body > main { visibility: hidden; }
    #signInGate { display: none; }
    html.launcher-gated #signInGate { display: flex; position: fixed; inset: 0; z-index: 50;
      align-items: center; justify-content: center; background: rgba(15,23,42,.45); }
    #signInGate .gate-card { background: #fff; border: 1px solid var(--line); border-radius: 12px;
      padding: 24px; max-width: 460px; width: calc(100% - 32px); box-shadow: 0 18px 48px rgba(15,23,42,.22); }
    #signInGate h2 { margin: 0 0 6px; font-size: 18px; }
    #signInGate p { margin: 0 0 14px; color: var(--muted); font-size: 13px; }
    @media (max-width: 680px) { .cards { grid-template-columns: 1fr; } }
  </style>
  <script>
    window.__LAUNCHER_CONFIG__ = __LAUNCHER_CONFIG_JSON__;
    (function(){ var c = window.__LAUNCHER_CONFIG__ || {};
      if (c.clerkConfigured && !c.devMode) { document.documentElement.classList.add("launcher-gated"); } })();
  </script>
</head>
<body>
  <div id="signInGate">
    <div class="gate-card">
      <h2>サインイン</h2>
      <p>このアカウントで在庫ダッシュボードと疑似freee の両方を使えます。</p>
      <div id="clerk-signin"></div>
      <p id="gate-msg" style="color:#b3261e;font-size:13px;"></p>
    </div>
  </div>
  <header>
    <h1>業務アプリ入口</h1>
    <div class="user-actions"><span id="launcher-user"></span></div>
  </header>
  <main>
    <p class="lead">使うアプリを選んでください。サインインは1回で、同じアカウントのまま両方を行き来できます。</p>
    <div class="cards">
      <a class="app-card inventory" href="__INVENTORY_URL__">
        <div class="icon">📦</div>
        <h2>在庫管理ダッシュボード</h2>
        <p>在庫・需要予測（AIモデル）・発注判定・仕入/売上請求書のAI取込。会計連携は「freee送信」で疑似freeeへ。</p>
        <span class="go">開く →</span>
      </a>
      __FREEE_CARD__
    </div>
  </main>
  <script>
    (function(){
      var CFG = window.__LAUNCHER_CONFIG__ || {};
      function feApi(pk){ var e = pk.split("_").slice(2).join("_"); try { return atob(e).replace(/\$$/, ""); } catch(_){ return ""; } }
      function loadClerk(pk){
        return new Promise(function(resolve, reject){
          var host = feApi(pk);
          if(!host){ reject(new Error("Clerk 公開キーの形式が不正です")); return; }
          var s = document.createElement("script");
          s.async = true; s.crossOrigin = "anonymous";
          s.setAttribute("data-clerk-publishable-key", pk);
          s.src = "https://" + host + "/npm/@clerk/clerk-js@5/dist/clerk.browser.js";
          s.onload = resolve;
          s.onerror = function(){ reject(new Error("Clerk スクリプトの読み込みに失敗しました")); };
          document.head.appendChild(s);
        });
      }
      function gate(){ document.documentElement.classList.add("launcher-gated"); }
      function ungate(){ document.documentElement.classList.remove("launcher-gated"); }
      async function boot(){
        if(!CFG.clerkConfigured || CFG.devMode){ ungate(); return; }
        gate();
        await loadClerk(CFG.clerkPublishableKey);
        await window.Clerk.load();
        function render(){
          if(window.Clerk.user){
            ungate();
            var u = document.getElementById("launcher-user");
            if(u){ u.innerHTML = ""; window.Clerk.mountUserButton(u); }
          } else {
            gate();
            var g = document.getElementById("clerk-signin");
            if(g && !g.hasChildNodes()){ window.Clerk.mountSignIn(g); }
          }
        }
        window.Clerk.addListener(render);
        render();
      }
      boot().catch(function(e){ var el = document.getElementById("gate-msg"); if(el){ el.textContent = e.message; } });
    })();
  </script>
</body>
</html>
"""

_FREEE_CARD_ENABLED = """<a class="app-card freee" href="__FREEE_URL__">
        <div class="icon">🧾</div>
        <h2>疑似freee 会計</h2>
        <p>在庫からの仕訳を受け取り、一般経費レシートをAIで入力。freee の取り込み画面を模した会計デモ。</p>
        <span class="go">開く →</span>
      </a>"""

_FREEE_CARD_DISABLED = """<div class="app-card freee disabled">
        <div class="icon">🧾</div>
        <h2>疑似freee 会計</h2>
        <p>会計デモ（在庫からの仕訳受け取り・レシートAI入力）。現在は未公開です。</p>
        <span class="go">準備中</span>
      </div>"""


def render_launcher(
    publishable_key: str = "",
    clerk_configured: bool = False,
    dev_mode: bool = False,
    pseudo_freee_url: str = "",
    inventory_url: str = "/",
) -> str:
    """入口ページの HTML を返す。

    pseudo_freee_url が空（未公開）のときは疑似freee カードを無効表示にする。
    秘密キーは渡さない（publishable_key のみブラウザに出す）。
    """
    config = {
        "clerkPublishableKey": publishable_key,
        "clerkConfigured": bool(clerk_configured),
        "devMode": bool(dev_mode),
    }
    config_json = json.dumps(config, ensure_ascii=False).replace("</", "<\\/")
    freee_url = (pseudo_freee_url or "").strip().rstrip("/")
    if freee_url:
        freee_card = _FREEE_CARD_ENABLED.replace("__FREEE_URL__", html.escape(freee_url))
    else:
        freee_card = _FREEE_CARD_DISABLED
    return (
        _LAUNCHER_TEMPLATE
        .replace("__LAUNCHER_CONFIG_JSON__", config_json)
        .replace("__INVENTORY_URL__", html.escape(inventory_url or "/"))
        .replace("__FREEE_CARD__", freee_card)
    )
