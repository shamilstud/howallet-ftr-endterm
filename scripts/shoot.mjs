// Screenshots with the Playwright already installed in ../../web (headless chromium).
//   node scripts/shoot.mjs url  <url> <out.png> [width] [height] [waitMs]
//   node scripts/shoot.mjs txt  <evidence.txt> <out.png> [title]     terminal-style render of a text file
//   node scripts/shoot.mjs alerts <prom /alerts url> <out.png> <alertname>
//   node scripts/shoot.mjs html <file.html> <out.png> [width] [height]
import { createRequire } from "node:module";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

const WEB = process.env.HOWALLET_WEB || resolve(process.cwd(), "../../web");
const require = createRequire(resolve(WEB, "package.json"));
const { chromium } = require("@playwright/test");

const [mode, src, out, a3, a4, a5] = process.argv.slice(2);
const esc = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

// The web/ Playwright may be newer than the installed browser build; fall back to the installed one.
import { existsSync, readdirSync } from "node:fs";
const pwDir = resolve(process.env.LOCALAPPDATA || "", "ms-playwright");
const shell = existsSync(pwDir)
  ? readdirSync(pwDir).filter((d) => d.startsWith("chromium_headless_shell-")).sort().pop()
  : null;
const exe = shell ? resolve(pwDir, shell, "chrome-headless-shell-win64", "chrome-headless-shell.exe") : undefined;
const browser = await chromium.launch(exe && existsSync(exe) ? { executablePath: exe } : {});
try {
  if (mode === "url") {
    const page = await browser.newPage({ viewport: { width: +a3 || 1600, height: +a4 || 1000 } });
    await page.goto(src, { waitUntil: "networkidle", timeout: 60000 }).catch(() => {});
    await page.waitForTimeout(+a5 || 4000);
    await page.screenshot({ path: out, fullPage: false });
  } else if (mode === "alerts") {
    // Prometheus /alerts with the named rule expanded (shows labels, state, "Active Since")
    const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
    await page.goto(src, { waitUntil: "networkidle", timeout: 60000 }).catch(() => {});
    await page.waitForTimeout(1200);
    if (a3) await page.getByText(a3, { exact: false }).first().click().catch(() => {});
    await page.waitForTimeout(800);
    await page.screenshot({ path: out, fullPage: true });
  } else if (mode === "html") {
    const page = await browser.newPage({ viewport: { width: +a3 || 1400, height: +a4 || 800 } });
    await page.goto(pathToFileURL(resolve(src)).href);
    await page.waitForTimeout(300);
    await page.screenshot({ path: out, fullPage: true });
  } else if (mode === "txt") {
    const text = readFileSync(src, "utf8").replace(/\r/g, "");
    const lines = text.split("\n");
    const width = Math.min(2200, Math.max(900, Math.max(...lines.map((l) => l.length)) * 7.3 + 60));
    const body = lines
      .map((l) => (l.startsWith("$ ") ? `<span class="p">$</span> <span class="c">${esc(l.slice(2))}</span>` : esc(l)))
      .join("\n");
    const html = `<!doctype html><meta charset="utf-8"><style>
      body{margin:0;background:#0d1117}
      .win{margin:0;border-radius:0;background:#0d1117}
      .bar{background:#161b22;color:#8b949e;font:12px/1 system-ui,sans-serif;padding:9px 14px;border-bottom:1px solid #30363d}
      .bar b{display:inline-block;width:11px;height:11px;border-radius:50%;margin-right:6px;vertical-align:-1px}
      pre{margin:0;padding:14px 18px;color:#c9d1d9;font:12px/1.45 Consolas,"Cascadia Mono",monospace;white-space:pre-wrap;word-break:break-all}
      .p{color:#3fb950}.c{color:#79c0ff}
    </style><div class="win"><div class="bar"><b style="background:#ff5f56"></b><b style="background:#ffbd2e"></b><b style="background:#27c93f"></b>&nbsp; ${esc(a3 || src.split(/[\\/]/).pop())}</div><pre>${body}</pre></div>`;
    const page = await browser.newPage({ viewport: { width: Math.round(width), height: 400 } });
    await page.setContent(html);
    await page.screenshot({ path: out, fullPage: true });
  }
  console.log("saved", out);
} finally {
  await browser.close();
}
