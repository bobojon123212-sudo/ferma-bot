// Полная проверка приложения «Ферма — учёт коров» на эмуляторе Android.
// Запуск: node android-test.mjs <метка>   (эмулятор уже запущен, debug-APK установлен)
import { _android as android } from "playwright";
import fs from "fs";

const LABEL = process.argv[2] || "android";
const OUT = new URL("./out/", import.meta.url).pathname;
fs.mkdirSync(OUT, { recursive: true });
const PKG = "tj.ferma.uchet";
const lines = [];
let fails = 0, warns = 0;
const log = s => { console.log(s); lines.push(s); };
const ok = (name, cond, detail = "") => {
  if (cond) log(`✅ ${name}`);
  else { fails++; log(`❌ ${name}${detail ? " — " + detail : ""}`); }
  return cond;
};
const warn = (name, detail = "") => { warns++; log(`⚠️ ${name}${detail ? " — " + detail : ""}`); };
const sleep = ms => new Promise(r => setTimeout(r, ms));
const near = (a, b) => Math.abs(a - b) < 0.01;

const [device] = await android.devices();
if (!device) { console.error("Нет устройства"); process.exit(2); }
const sh = async cmd => (await device.shell(cmd)).toString();
log(`Устройство: ${device.model()} · Android SDK ${(await sh("getprop ro.build.version.sdk")).trim()} · WebView ${(await sh("dumpsys webviewupdate | grep 'Current WebView package'")).trim()}`);

let page, jsErrors = [];
async function launch(fresh = false) {
  await sh(`am force-stop ${PKG}`);
  if (fresh) await sh(`pm clear ${PKG}`);
  await sh(`am start -W -n ${PKG}/.MainActivity`);
  const wv = await device.webView({ pkg: PKG }, { timeout: 60000 });
  page = await wv.page();
  page.on("pageerror", e => jsErrors.push(String(e)));
  page.on("console", m => { if (m.type() === "error") jsErrors.push(m.text()); });
  await page.waitForSelector("#view", { timeout: 30000 });
  await sleep(800);
}
const shot = async name => { try { await device.screenshot({ path: `${OUT}${LABEL}-${name}.png` }); } catch (e) { warn(`скриншот ${name}`, e.message); } };
const ev = (fn, arg) => page.evaluate(fn, arg);
const db = () => ev(() => JSON.parse(JSON.stringify(DB)));
const toastText = async () => { await sleep(250); return ev(() => { const t = document.querySelector(".toast"); return t ? t.textContent : ""; }); };
const click = async (sel, opts) => { await page.click(sel, Object.assign({ timeout: 8000 }, opts)); await sleep(250); };
const fill = async (sel, v) => { await page.fill(sel, v, { timeout: 8000 }); };
const sheets = () => ev(() => S.sheets.length);
const back = async () => { await sh("input keyevent 4"); await sleep(900); };
const focusedApp = async () => (await sh("dumpsys window | grep -E 'mCurrentFocus|mFocusedApp'")).trim();
async function uiTap(re) {
  // нажать системную кнопку (окно выбора файла и т.п.) по тексту
  await sh("uiautomator dump /sdcard/ui.xml >/dev/null 2>&1");
  const xml = await sh("cat /sdcard/ui.xml");
  const nodes = [...xml.matchAll(/<node [^>]*?text="([^"]*)"[^>]*?bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"/g)];
  const n = nodes.find(m => re.test(m[1]));
  if (!n) return false;
  const x = (Number(n[2]) + Number(n[4])) >> 1, y = (Number(n[3]) + Number(n[5])) >> 1;
  await sh(`input tap ${x} ${y}`);
  await sleep(1500);
  return true;
}

// настоящее касание пальцем (adb), чтобы появилась клавиатура
async function nativeTap(sel) {
  await sh("uiautomator dump /sdcard/ui.xml >/dev/null 2>&1");
  const xml = await sh("cat /sdcard/ui.xml");
  const m = xml.match(/class="android\.webkit\.WebView"[^>]*?bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"/);
  const r = await ev(q => { const b = document.querySelector(q).getBoundingClientRect(); return { x: b.left + b.width / 2, y: b.top + b.height / 2, dpr: devicePixelRatio }; }, sel);
  const left = m ? +m[1] : 0, top = m ? +m[2] : 0;
  await sh(`input tap ${Math.round(left + r.x * r.dpr)} ${Math.round(top + r.y * r.dpr)}`);
}

try {
  // ═══ 1. Первый запуск ═══
  log("\n— 1. Первый запуск —");
  await launch(true);
  await shot("01-welcome");
  ok("Мост Android подключён (данные пишутся в память телефона)", await ev(() => HAS_NATIVE));
  ok("Приложение пустое: нет коров/расходов/склада", await ev(() => DB.cows.length === 0 && DB.expenses.length === 0 && DB.stock.length === 0));
  ok("Экран приветствия показан", await page.isVisible(".welcome"));
  ok("Корма по умолчанию есть (5 шт.)", (await db()).products.length === 5);
  const insets = await ev(() => ({ top: document.querySelector("header").getBoundingClientRect().top, navBottom: document.querySelector("#tabs").getBoundingClientRect().bottom, vh: window.innerHeight }));
  ok("Шапка не уходит под статус-бар (верх страницы = 0)", insets.top === 0, JSON.stringify(insets));
  ok("Нижнее меню целиком на экране", insets.navBottom <= insets.vh + 1, JSON.stringify(insets));

  // ═══ 2. Добавление коров + проверки ввода ═══
  log("\n— 2. Добавление коров —");
  await click(".welcome [data-act=cow-add]");
  await click(".sheet [data-submit]");
  ok("Пустое имя → ошибка", /имя/i.test(await toastText()), await toastText());
  await fill(".sheet [name=name]", "Бурёнка");
  await fill(".sheet [name=price]", "абв");
  await click(".sheet [data-submit]");
  ok("Цена буквами → ошибка", /Неверное значение|цен/i.test(await toastText()), await toastText());
  await fill(".sheet [name=price]", "0");
  await click(".sheet [data-submit]");
  ok("Цена 0 → ошибка", /больше нуля/.test(await toastText()), await toastText());
  await fill(".sheet [name=price]", "8 000");
  await fill(".sheet [name=date]", "2099-01-01");
  await click(".sheet [data-submit]");
  ok("Дата в будущем → ошибка", /будущем/.test(await toastText()), await toastText());
  await fill(".sheet [name=date]", "2026-07-14");
  await fill(".sheet [name=weight]", "310,5");
  await fill(".sheet [name=note]", "чёрно-белая <b>тест</b>");
  await shot("02-cow-form");
  await click(".sheet [data-submit]");
  let d = await db();
  const c1 = d.cows[0];
  ok("Корова 1 сохранена", d.cows.length === 1 && c1.name === "Бурёнка");
  ok("Цена «8 000» → 8000", c1.buy_price === 8000, String(c1.buy_price));
  ok("Вес «310,5» → 310.5", c1.buy_weight === 310.5, String(c1.buy_weight));
  ok("Дата покупки 14.07.2026", c1.buy_date.startsWith("2026-07-14"), c1.buy_date);
  ok("После добавления открыта карточка коровы", await page.isVisible(".sheet .kv"));
  ok("HTML в заметке не выполняется (экранирован)", await ev(() => !document.querySelector(".sheet .hint b") && document.querySelector(".sheet").textContent.includes("<b>тест</b>")));
  const days = await ev(() => findCow(DB.cows[0].id).days);
  const expDays = Math.round((Date.UTC(...(await ev(() => todayStr())).split("-").map((v, i) => i === 1 ? v - 1 : +v)) - Date.UTC(2026, 6, 14)) / 864e5) + 1;
  ok("Дней в хозяйстве считается верно", days === expDays, `${days} vs ${expDays}`);
  await shot("03-cow-card");
  await back();
  ok("«Назад» закрывает карточку", (await sheets()) === 0);

  await click("[data-tab=add]");
  await click(".sheet [data-act=cow-add]");
  await fill(".sheet [name=name]", "Чёрная");
  await fill(".sheet [name=price]", "6500.5");
  await click(".sheet [data-submit]");
  await back();
  d = await db();
  ok("Корова 2 сохранена, цена 6500.5", d.cows.length === 2 && d.cows[1].buy_price === 6500.5);
  const [A, B] = d.cows.map(c => c.id);

  // ═══ 3. Расходы ═══
  log("\n— 3. Расходы —");
  await click("[data-tab=home]");
  await click("[data-act=expense]");
  await click(".sheet [data-submit]");
  ok("Без коровы → ошибка", /корову/i.test(await toastText()));
  await click(`.sheet .chips[data-name=cow] .chip[data-v="${A}"]`);
  await click(".sheet [data-submit]");
  ok("Без категории → ошибка", /категори/i.test(await toastText()));
  await click('.sheet .chips[data-name=category] .chip[data-v="💊 Лекарства"]');
  await fill(".sheet [name=amount]", "-5");
  await click(".sheet [data-submit]");
  ok("Отрицательная сумма → ошибка", /больше нуля/.test(await toastText()));
  await fill(".sheet [name=amount]", "150");
  await fill(".sheet [name=note]", "прививка");
  await click(".sheet [data-submit]");
  d = await db();
  ok("Расход 150 на Бурёнку записан", d.expenses.length === 1 && d.expenses[0].amount === 150 && d.expenses[0].cow_id === A && d.expenses[0].note === "прививка");

  await click("[data-tab=add]");
  await click('.sheet [data-act=expense][data-id=all]');
  await click('.sheet .chip[data-v="🌾 Отруб"]');
  await fill(".sheet [name=amount]", "401");
  await shot("04-expense-all");
  await click(".sheet [data-submit]");
  d = await db();
  const shares = d.expenses.filter(e => e.category === "🌾 Отруб");
  ok("Расход на всех: 2 записи по 200.5", shares.length === 2 && shares.every(e => near(e.amount, 200.5)), JSON.stringify(shares.map(e => e.amount)));
  const home = await ev(() => L.summary());
  ok("Главная: расход сегодня = 551", near(home.today_total, 551), String(home.today_total));
  ok("Главная: вложено = 8000+6500.5+551 = 15051.5", near(home.invested, 15051.5), String(home.invested));
  await shot("05-home");

  // ═══ 4. Корма и товары ═══
  log("\n— 4. Корма и товары —");
  await click("[data-tab=more]");
  await click("[data-act=products]");
  await click(".sheet [data-act=product-add]");
  await fill(".sheet [name=name]", "Ячмень");
  await click('.sheet .chip[data-v="мешок"]');
  await click(".sheet [data-submit]");
  d = await db();
  ok("Корм «Ячмень» (мешок) добавлен", d.products.some(p => p.name === "Ячмень" && p.unit === "мешок"));
  await click(".sheet [data-act=product-add]");
  await fill(".sheet [name=name]", "Ячмень");
  await click(".sheet [data-submit]");
  ok("Дубликат корма → ошибка", /уже есть/.test(await toastText()));
  await back();
  ok("Ячмень есть в категориях расходов", await ev(() => allCategories().includes("Ячмень")));
  await back();

  // ═══ 5. Склад ═══
  log("\n— 5. Склад —");
  await click("[data-tab=stock]");
  await click("[data-act=stock-buy]");
  await click('.sheet .chip[data-v="Ячмень"]');
  ok("Единица меняется на «мешок» в подписи", /мешок/.test(await ev(() => document.querySelector(".sheet .f:nth-of-type(2) > span").textContent)));
  await fill(".sheet [name=kg]", "20");
  await fill(".sheet [name=price]", "3000");
  await click(".sheet [data-submit]");
  d = await db();
  ok("Закупка: 20 мешков за 3000", d.stock.length === 1 && d.stock[0].kg_total === 20 && d.stock[0].unit === "мешок");
  await click("[data-act=writeoff]");
  await fill(".sheet [name=kg]", "25");
  await click(".sheet [data-submit]");
  ok("Списать больше, чем есть → ошибка", /только 20/.test(await toastText()), await toastText());
  await fill(".sheet [name=kg]", "17");
  await click(".sheet [data-submit]");
  d = await db();
  const wo = d.expenses.filter(e => e.category === "Ячмень");
  ok("Списание 17 мешков: остаток 3", near(d.stock[0].kg_left, 3), String(d.stock[0].kg_left));
  ok("Стоимость 17×150 = 2550 → по 1275 на корову", wo.length === 2 && wo.every(e => near(e.amount, 1275)), JSON.stringify(wo.map(e => e.amount)));
  ok("Предупреждение «мало корма» показано", await page.isVisible(".sheet .warn"));
  await shot("06-writeoff");
  await click(".sheet [data-act=close]");
  ok("Карточка склада: 15% и метка «мало»", await ev(() => document.querySelector("#view").textContent.includes("(15%)") && !!document.querySelector("#view .pill.red")));
  await shot("07-stock");

  // ═══ 6. Сохранение после перезапуска ═══
  log("\n— 6. Сохранение данных после закрытия приложения —");
  const before = JSON.stringify(await db());
  await launch(false);
  const after = JSON.stringify(await db());
  ok("Все данные на месте после принудительного закрытия", before === after);
  ok("data.json лежит во внутренней памяти приложения", /data\.json/.test(await sh(`run-as ${PKG} ls files`)));

  // ═══ 7. Фото ═══
  log("\n— 7. Фото —");
  const jpg = "/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAAMCAgICAgMCAgIDAwMDBAYEBAQEBAgGBgUGCQgKCgkICQkKDA8MCgsOCwkJDRENDg8QEBEQCgwSExIQEw8QEBD/yQALCAABAAEBAREA/8wABgAQEAX/2gAIAQEAAD8A0s8g/9k=";
  const url = await ev(b => Android.savePhoto(b), jpg);
  ok("Фото сохраняется в память телефона (file://…/photos/…jpg)", /^file:\/\/.*\/files\/photos\/.+\.jpg$/.test(url), url);
  ok("Фото читается обратно без потерь", (await ev(u => Android.readPhoto(u), url)) === jpg);
  ok("Защита: нельзя прочитать чужой файл через фото", (await ev(() => Android.readPhoto("file:///data/data/tj.ferma.uchet/files/data.json"))) === "");
  ok("Защита: путь с ../ не срабатывает", (await ev(u => Android.readPhoto(u.replace(/photos\/.*/, "photos/../data.json")), url)) === "");
  await ev(([u, id]) => { DB.photos.push({ id: nextId(), cow_id: id, url: u, added_at: nowIso() }); saveDB(); refresh(); }, [url, A]);
  await click("[data-tab=cows]");
  await click(`.cow[data-id="${A}"]`);
  await sleep(800);
  ok("Фото отображается в карточке коровы", await ev(() => { const i = document.querySelector(".sheet .photos img"); return !!i && i.complete && i.naturalWidth > 0; }));
  ok("Миниатюра в списке коров загружается", await ev(() => getComputedStyle(document.querySelector(".cow .thumb")).backgroundImage.includes("file://")));
  // настоящее окно выбора фото Android
  await click(".sheet [data-act=photo-add]");
  await sleep(2500);
  const pickFocus = await focusedApp();
  ok("Кнопка «Фото» открывает системный выбор файлов", !/MainActivity/.test(pickFocus.split("\n").find(l => l.includes("mCurrentFocus")) || ""), pickFocus);
  await shot("08-photo-picker");
  await back();
  await sleep(1000);
  ok("После отмены выбора приложение живо и карточка открыта", (await focusedApp()).includes(PKG) && (await sheets()) === 1);
  await shot("09-cow-photo");
  await click(".sheet .photos img");
  await click(".viewer [data-del]");
  await click("[data-yes]");
  d = await db();
  ok("Фото удалено из базы", d.photos.length === 0);
  ok("Файл фото удалён с телефона", (await ev(u => Android.readPhoto(u), url)) === "");

  // ═══ 8. Удаление записи расхода ═══
  log("\n— 8. Удаление записи расхода —");
  const nExp = (await db()).expenses.length;
  await click(".sheet [data-act=exp-del]");
  await click("[data-yes]");
  ok("Запись расхода удалена", (await db()).expenses.length === nExp - 1);
  await back();

  // ═══ 9. Продажа ═══
  log("\n— 9. Продажа —");
  d = await db();
  const expA = d.expenses.filter(e => e.cow_id === A).reduce((a, e) => a + e.amount, 0);
  await click(`.cow[data-id="${A}"]`);
  await click(".sheet [data-act=sell]");
  await fill(".sheet [name=price]", "12000");
  await fill(".sheet [name=weight]", "395");
  await click(".sheet [data-submit]");
  const profitShown = await ev(() => document.querySelector(".sheet .result .big").textContent);
  const expProfit = 12000 - 8000 - expA;
  ok(`Прибыль = 12000 − 8000 − ${expA} = ${expProfit}`, profitShown.replace(/\s/g, "").includes(String(Math.round(expProfit))), profitShown);
  await shot("10-sold");
  await click(".sheet [data-act=close]");
  d = await db();
  ok("Корова в статусе «продана»", d.cows.find(c => c.id === A).status === "sold");
  await click("[data-tab=home]");
  await click("[data-act=expense]");
  ok("Проданной коровы нет в выборе для расхода", !(await page.isVisible(`.sheet .chip[data-v="${A}"]`)));
  await back();
  await click("[data-tab=cows]");
  await click("[data-act=cows-tab][data-id=sold]");
  ok("Вкладка «Проданные» показывает прибыль", (await ev(() => { const e = document.querySelector("#view .cow .pos"); return e ? e.textContent : ""; })).length > 0);
  await click(`.cow[data-id="${A}"]`);
  await click(".sheet [data-act=cow-unsell]");
  await click("[data-yes]");
  ok("Отмена продажи возвращает корову в активные", (await db()).cows.find(c => c.id === A).status === "active");
  await back();

  // ═══ 10. Отчёты ═══
  log("\n— 10. Отчёты —");
  await click("[data-tab=more]");
  await click("[data-act=today]");
  ok("Отчёт за сегодня открылся", await page.isVisible(".sheet .card"));
  await back();
  await click("[data-act=month]");
  const m0 = await ev(() => S.month);
  await click('.sheet .month-nav button >> nth=0');
  const m1 = await ev(() => S.month);
  ok("Листание месяцев назад", m1 < m0, `${m0} → ${m1}`);
  ok("Только одно окно отчёта после листания", (await sheets()) === 1);
  await click('.sheet .month-nav button >> nth=1');
  ok("Итог месяца = сумма всех расходов", await ev(() => Math.abs(L.month(S.month).total - DB.expenses.reduce((a, e) => a + e.amount, 0)) < 0.01));
  await shot("11-month");
  await back();
  await click("[data-act=alltime]");
  ok("Отчёт за всё время открылся", await page.isVisible(".sheet .card"));
  await back();

  // ═══ 11. Валюта ═══
  log("\n— 11. Валюта —");
  await click("[data-act=currency]");
  await click('.sheet .chip[data-id="сум"]');
  ok("Валюта «сум» сохранена", (await db()).settings.currency === "сум");
  await click("[data-tab=home]");
  ok("Подпись валюты на главной поменялась", await ev(() => document.querySelector(".stat.big").textContent.includes("сум")));

  // ═══ 12. Поворот экрана ═══
  log("\n— 12. Поворот экрана —");
  await click("[data-act=expense]");
  await sh("settings put system accelerometer_rotation 0; settings put system user_rotation 1");
  await sleep(2500);
  await shot("12-landscape");
  ok("После поворота окно не закрылось и данные целы", (await sheets()) === 1 && (await db()).cows.length === 2);
  await sh("settings put system user_rotation 0");
  await sleep(2000);
  await back();

  // ═══ 13. Клавиатура ═══
  log("\n— 13. Клавиатура —");
  await click("[data-tab=add]");
  await click(".sheet [data-act=cow-add]");
  const h0 = await ev(() => window.innerHeight);
  await nativeTap(".sheet [name=price]");
  await sleep(2000);
  const h1 = await ev(() => window.innerHeight);
  await shot("13-keyboard");
  if (h1 < h0) ok("Клавиатура сдвигает экран (поля не закрываются)", true);
  else warn("Клавиатура на эмуляторе не появилась или не изменила высоту", `${h0} → ${h1}`);
  await back(); await back();
  if ((await sheets()) > 0) await back();

  // ═══ 14. Резервная копия ═══
  log("\n— 14. Резервная копия —");
  const snap = await db();
  await click("[data-tab=more]");
  await click("[data-act=backup]");
  await sleep(3000);
  await shot("14-backup-save");
  const saved = await uiTap(/^(SAVE|Save|Сохранить|СОХРАНИТЬ)$/);
  if (saved) {
    const t = await toastText();
    ok("Копия сохранена в файл через системное окно", /Копия сохранена/.test(t), t);
  } else { warn("Не нашёл кнопку «Сохранить» в системном окне"); await back(); }
  // восстановление: проверяем сам механизм (как после выбора файла)
  const backupJson = await ev(() => {
    const photos = {};
    DB.photos.forEach(p => { const b = readPhotoB64(p.url); if (b) photos[p.id] = b; });
    return JSON.stringify({ app: "ferma-uchet", version: 1, exported_at: nowIso(), db: DB, photos });
  });
  await click("[data-act=wipe]");
  await click("[data-yes]");
  await click("[data-yes]");
  ok("«Удалить все данные» очищает приложение", await ev(() => DB.cows.length === 0 && DB.expenses.length === 0) && await page.isVisible(".welcome"));
  ok("Валюта сохраняется после очистки", (await db()).settings.currency === "сум");
  await ev(t => onBackupFile("не json"), "");
  ok("Неверный файл копии → понятная ошибка", /не файл копии/.test(await toastText()));
  await ev(t => onBackupFile(t), backupJson);
  const restored = await db();
  ok("Восстановление из копии: все коровы и расходы вернулись",
    restored.cows.length === snap.cows.length && restored.expenses.length === snap.expenses.length && restored.stock.length === snap.stock.length,
    `${restored.cows.length}/${restored.expenses.length}/${restored.stock.length}`);
  if (saved) {
    await click("[data-tab=more]");
    await click("[data-act=restore]");
    await click("[data-yes]");
    await sleep(3000);
    await shot("15-restore-pick");
    const picked = await uiTap(/ferma-kopiya-.*\.json/);
    if (picked) {
      await sleep(1500);
      ok("Восстановление через выбор файла на телефоне", /Восстановлено/.test(await toastText()), await toastText());
    } else { warn("Файл копии не найден в системном окне выбора"); await back(); }
  }
  await launch(false);
  ok("Восстановленные данные сохранены после перезапуска", (await db()).cows.length === snap.cows.length);

  // ═══ 15. Удаление коровы ═══
  log("\n— 15. Удаление коровы —");
  d = await db();
  await click("[data-tab=cows]");
  await click(`.cow[data-id="${B}"]`);
  await click(".sheet [data-act=cow-del]");
  await click("[data-no]");
  ok("«Отмена» не удаляет корову", (await db()).cows.length === 2);
  await click(".sheet [data-act=cow-del]");
  await click("[data-yes]");
  d = await db();
  ok("Корова удалена вместе с её расходами", d.cows.length === 1 && !d.expenses.some(e => e.cow_id === B));

  // ═══ 16. Кнопка «Назад» ═══
  log("\n— 16. Кнопка «Назад» —");
  await click("[data-tab=stock]");
  await back();
  ok("«Назад» на вкладке → Главная", (await ev(() => S.tab)) === "home");
  await back();
  await sleep(1500);
  ok("«Назад» на главной закрывает приложение", !(await focusedApp()).includes(`${PKG}/`), await focusedApp());
  await sh(`am start -W -n ${PKG}/.MainActivity`);
  await sleep(2500);
  ok("После повторного открытия данные на месте", /data\.json/.test(await sh(`run-as ${PKG} ls files`)));

  ok("Нет ошибок JavaScript", jsErrors.length === 0, jsErrors.slice(0, 5).join(" | "));
} catch (e) {
  fails++;
  log(`❌ ТЕСТ ПРЕРВАН: ${e.message.split("\n")[0]}`);
  await shot("zz-crash");
  log((await sh(`logcat -d -t 80 | grep -iE 'AndroidRuntime|FATAL|ferma' || true`)).slice(-3000));
}

log(`\nИТОГ ${LABEL}: ошибок ${fails}, предупреждений ${warns}`);
fs.writeFileSync(`${OUT}${LABEL}-report.txt`, lines.join("\n"));
await device.close();
process.exit(fails ? 1 : 0);
