// Набор текста настоящими касаниями и клавишами (adb input) в приложении на эмуляторе.
// node tests/typing.mjs <пакет> <метка>
import { _android as android } from "playwright";
import fs from "fs";
const [PKG, L] = process.argv.slice(2);
const OUT = "tests/out/"; fs.mkdirSync(OUT, { recursive: true });
const rep = []; let fails = 0;
const ok = (n, c, d = "") => { rep.push(`${c ? "✅" : "❌"} ${L}: ${n}${c ? "" : " — " + d}`); if (!c) fails++; };
const sleep = ms => new Promise(r => setTimeout(r, ms));
const [dev] = await android.devices();
const sh = async c => (await dev.shell(c)).toString();
await sh(`am force-stop ${PKG}`); await sh(`pm clear ${PKG}`);
await sh(`am start -W -n ${PKG}/.MainActivity`);
const page = await (await dev.webView({ pkg: PKG }, { timeout: 120000 })).page();
await page.waitForSelector("#login:not(.hidden) .btn, #view .card", { timeout: 120000 });
await sleep(1500);
async function realTap(sel) {
  await sh("uiautomator dump /sdcard/ui.xml >/dev/null 2>&1");
  const m = (await sh("cat /sdcard/ui.xml")).match(/class="android\.webkit\.WebView"[^>]*?bounds="\[(\d+),(\d+)\]/);
  const r = await page.evaluate(q => { const e = document.querySelector(q); e.scrollIntoView({ block: "center" }); const b = e.getBoundingClientRect(); return { x: b.left + b.width / 2, y: b.top + b.height / 2, d: devicePixelRatio }; }, sel);
  const x = (m ? +m[1] : 0) + r.x * r.d, y = (m ? +m[2] : 0) + r.y * r.d;
  await sh(`input tap ${Math.round(x)} ${Math.round(y)}`); await sleep(1500);
}
async function typeInto(sel, digits, name) {
  await realTap(sel);
  const focused = await page.evaluate(q => document.activeElement === document.querySelector(q), sel);
  const ime = (await sh("dumpsys input_method | grep -o 'mInputShown=[a-z]*' | head -1")).trim();
  await sh(`input text ${digits}`); await sleep(1500);
  const v = await page.inputValue(sel);
  ok(`${name}: набрано «${v}»`, v.replace(/\s/g, "") === digits, `поле в фокусе: ${focused}, ${ime}`);
}
try {
  if (PKG === "tj.ferma.app") {
    await page.click("#byid"); await sleep(800);
    await typeInto("#uid", "6985425925", "Telegram ID");
    await dev.screenshot({ path: `${OUT}${L}-typed.png` });
  } else {
    await page.click(".welcome [data-act=cow-add]"); await sleep(800);
    await typeInto(".sheet [name=price]", "6500", "Цена коровы");
    await typeInto(".sheet [name=weight]", "280", "Вес");
    await dev.screenshot({ path: `${OUT}${L}-typed.png` });
    await page.fill(".sheet [name=name]", "Test"); await page.click(".sheet [data-submit]"); await sleep(800);
    await page.click(".sheet [data-act=expense]"); await sleep(800);
    await page.click('.sheet .chip[data-v="💊 Лекарства"]');
    await typeInto(".sheet [name=amount]", "150", "Сумма расхода");
  }
} catch (e) { ok("тест прерван", false, e.message.split("\n")[0]); }
fs.appendFileSync(`${OUT}keyboard-report.txt`, rep.join("\n") + "\n");
console.log(rep.join("\n"));
await dev.close();
