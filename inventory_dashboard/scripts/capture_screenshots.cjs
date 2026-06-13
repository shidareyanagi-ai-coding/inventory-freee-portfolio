const fs = require("fs");
const http = require("http");
const os = require("os");
const path = require("path");
const { spawn } = require("child_process");

const root = path.resolve(__dirname, "..");
const outDir = path.join(root, "screenshots");
const url = "http://127.0.0.1:8000/";
const port = 9223;
const edgePath = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const chromePath = "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";
const browserPath = fs.existsSync(edgePath) ? edgePath : chromePath;

fs.mkdirSync(outDir, { recursive: true });

function wait(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function getJson(targetUrl) {
  return new Promise((resolve, reject) => {
    http
      .get(targetUrl, (res) => {
        let body = "";
        res.on("data", (chunk) => (body += chunk));
        res.on("end", () => {
          try {
            resolve(JSON.parse(body));
          } catch (error) {
            reject(error);
          }
        });
      })
      .on("error", reject);
  });
}

async function waitForVersion() {
  for (let i = 0; i < 50; i += 1) {
    try {
      return await getJson(`http://127.0.0.1:${port}/json/version`);
    } catch {
      await wait(100);
    }
  }
  throw new Error("Browser did not expose DevTools endpoint.");
}

function connect(webSocketDebuggerUrl) {
  const ws = new WebSocket(webSocketDebuggerUrl);
  let id = 0;
  const pending = new Map();
  const listeners = new Map();

  ws.onmessage = (event) => {
    const message = JSON.parse(event.data);
    if (message.id && pending.has(message.id)) {
      const { resolve, reject } = pending.get(message.id);
      pending.delete(message.id);
      if (message.error) reject(new Error(message.error.message));
      else resolve(message.result || {});
      return;
    }
    if (message.method && listeners.has(message.method)) {
      listeners.get(message.method)(message.params || {});
    }
  };

  return new Promise((resolve, reject) => {
    ws.onopen = () => {
      resolve({
        send(method, params = {}) {
          const callId = ++id;
          ws.send(JSON.stringify({ id: callId, method, params }));
          return new Promise((resolveCall, rejectCall) => {
            pending.set(callId, { resolve: resolveCall, reject: rejectCall });
          });
        },
        once(method) {
          return new Promise((resolveEvent) => {
            listeners.set(method, (params) => {
              listeners.delete(method);
              resolveEvent(params);
            });
          });
        },
        close() {
          ws.close();
        },
      });
    };
    ws.onerror = reject;
  });
}

async function capture(client, fileName, clip) {
  const result = await client.send("Page.captureScreenshot", {
    format: "png",
    fromSurface: true,
    captureBeyondViewport: true,
    clip: {
      x: Math.max(0, Math.floor(clip.x)),
      y: Math.max(0, Math.floor(clip.y)),
      width: Math.max(1, Math.ceil(clip.width)),
      height: Math.max(1, Math.ceil(clip.height)),
      scale: 1,
    },
  });
  fs.writeFileSync(path.join(outDir, fileName), Buffer.from(result.data, "base64"));
}

async function sectionClip(client, text) {
  const expression = `
    (() => {
      const sections = [...document.querySelectorAll("section")];
      const section = sections.find((node) => node.innerText.includes(${JSON.stringify(text)}));
      if (!section) return null;
      const rect = section.getBoundingClientRect();
      return {
        x: rect.left + window.scrollX - 8,
        y: rect.top + window.scrollY - 8,
        width: rect.width + 16,
        height: rect.height + 16
      };
    })()
  `;
  const result = await client.send("Runtime.evaluate", {
    expression,
    returnByValue: true,
  });
  if (!result.result.value) {
    throw new Error(`Section not found: ${text}`);
  }
  return result.result.value;
}

(async () => {
  if (!fs.existsSync(browserPath)) {
    throw new Error("Edge or Chrome was not found.");
  }

  const userDataDir = fs.mkdtempSync(path.join(os.tmpdir(), "inventory-screenshots-"));
  const browser = spawn(browserPath, [
    "--headless=new",
    `--remote-debugging-port=${port}`,
    `--user-data-dir=${userDataDir}`,
    "--disable-gpu",
    "--no-first-run",
    "--no-default-browser-check",
    "about:blank",
  ]);

  try {
    const version = await waitForVersion();
    const client = await connect(version.webSocketDebuggerUrl);
    await client.send("Target.createTarget", { url: "about:blank" });
    const targets = await getJson(`http://127.0.0.1:${port}/json`);
    const pageTarget = targets.find((target) => target.type === "page");
    const pageClient = await connect(pageTarget.webSocketDebuggerUrl);

    await pageClient.send("Page.enable");
    await pageClient.send("Runtime.enable");
    await pageClient.send("Emulation.setDeviceMetricsOverride", {
      width: 1440,
      height: 1000,
      deviceScaleFactor: 1,
      mobile: false,
    });
    const loaded = pageClient.once("Page.loadEventFired");
    await pageClient.send("Page.navigate", { url });
    await loaded;
    await wait(1000);

    const metrics = await pageClient.send("Page.getLayoutMetrics");
    const content = metrics.contentSize;
    await capture(pageClient, "dashboard-full-page.png", {
      x: 0,
      y: 0,
      width: content.width,
      height: content.height,
    });
    await capture(pageClient, "dashboard-overview.png", {
      x: 0,
      y: 0,
      width: 1440,
      height: 1000,
    });

    await capture(pageClient, "inventory-and-monthly-summary.png", await sectionClip(pageClient, "在庫一覧"));
    await capture(pageClient, "forecast-simulation.png", await sectionClip(pageClient, "適正在庫シミュレーション"));
    await capture(pageClient, "inventory-ledger.png", await sectionClip(pageClient, "在庫元帳"));
    await capture(pageClient, "freee-queue-and-preview.png", await sectionClip(pageClient, "freee送信待ちキュー"));

    pageClient.close();
    client.close();
  } finally {
    browser.kill();
  }
})();
