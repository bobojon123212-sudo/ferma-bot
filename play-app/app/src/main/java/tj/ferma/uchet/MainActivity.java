package tj.ferma.uchet;

import android.app.Activity;
import android.content.ActivityNotFoundException;
import android.content.Intent;
import android.graphics.Color;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.util.Base64;
import android.view.View;
import android.view.ViewGroup;
import android.view.WindowInsets;
import android.webkit.JavascriptInterface;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.FrameLayout;
import android.widget.LinearLayout;

import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.nio.charset.StandardCharsets;
import java.util.UUID;

/**
 * «Ферма — учёт коров». Полностью офлайн: интерфейс лежит в assets/www,
 * данные (data.json) и фото (photos/) хранятся во внутренней памяти приложения.
 */
public class MainActivity extends Activity {

    private static final int REQ_PHOTO = 1;
    private static final int REQ_EXPORT = 2;
    private static final int REQ_IMPORT = 3;
    private static final String START = "file:///android_asset/www/index.html";

    private WebView web;
    private View statusBg;
    private ValueCallback<Uri[]> fileCallback;
    private String pendingExport;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        // Разметка: зелёная полоса под статус-баром + WebView. Отступы от системных панелей
        // считаем сами (Android 15+ всегда рисует приложение «от края до края»).
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setBackgroundColor(Color.WHITE);
        statusBg = new View(this);
        statusBg.setBackgroundColor(Color.parseColor("#2F5D3A"));
        root.addView(statusBg, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0));
        web = new WebView(this);
        web.setBackgroundColor(Color.parseColor("#F4F1EA"));
        root.addView(web, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f));
        setContentView(root);

        root.setOnApplyWindowInsetsListener((v, insets) -> {
            int top, bottom, left, right;
            if (Build.VERSION.SDK_INT >= 30) {
                android.graphics.Insets bars = insets.getInsets(WindowInsets.Type.systemBars() | WindowInsets.Type.displayCutout());
                android.graphics.Insets ime = insets.getInsets(WindowInsets.Type.ime());
                top = bars.top; left = bars.left; right = bars.right;
                bottom = Math.max(bars.bottom, ime.bottom);
            } else {
                top = insets.getSystemWindowInsetTop();
                bottom = insets.getSystemWindowInsetBottom();
                left = insets.getSystemWindowInsetLeft();
                right = insets.getSystemWindowInsetRight();
            }
            ViewGroup.LayoutParams lp = statusBg.getLayoutParams();
            lp.height = top;
            statusBg.setLayoutParams(lp);
            v.setPadding(left, 0, right, bottom);
            return Build.VERSION.SDK_INT >= 30 ? WindowInsets.CONSUMED : insets.consumeSystemWindowInsets();
        });
        if (Build.VERSION.SDK_INT >= 30) {
            getWindow().setDecorFitsSystemWindows(false);
        } else {
            root.setFitsSystemWindows(false);
            getWindow().getDecorView().setSystemUiVisibility(
                    View.SYSTEM_UI_FLAG_LAYOUT_STABLE | View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN
                            | (Build.VERSION.SDK_INT >= 27 ? View.SYSTEM_UI_FLAG_LIGHT_NAVIGATION_BAR : 0));
            getWindow().setStatusBarColor(Color.TRANSPARENT);
        }
        if (Build.VERSION.SDK_INT >= 30 && getWindow().getInsetsController() != null) {
            // светлые (белые) значки в статус-баре, тёмные — в нижней панели
            getWindow().getInsetsController().setSystemBarsAppearance(
                    android.view.WindowInsetsController.APPEARANCE_LIGHT_NAVIGATION_BARS,
                    android.view.WindowInsetsController.APPEARANCE_LIGHT_STATUS_BARS
                            | android.view.WindowInsetsController.APPEARANCE_LIGHT_NAVIGATION_BARS);
        }

        // Отладка WebView только в тестовой (debug) сборке — в версии для Google Play выключена
        if ((getApplicationInfo().flags & android.content.pm.ApplicationInfo.FLAG_DEBUGGABLE) != 0) {
            WebView.setWebContentsDebuggingEnabled(true);
        }

        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setAllowFileAccess(true);           // фото из внутренней памяти (file://…/photos)
        s.setAllowContentAccess(false);
        s.setTextZoom(100);
        web.addJavascriptInterface(new Bridge(), "Android");

        web.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                Uri url = request.getUrl();
                if ("file".equals(url.getScheme())) return false;
                try { startActivity(new Intent(Intent.ACTION_VIEW, url)); } catch (ActivityNotFoundException ignored) { }
                return true;
            }
        });

        web.setWebChromeClient(new WebChromeClient() {
            @Override
            public boolean onShowFileChooser(WebView view, ValueCallback<Uri[]> callback, FileChooserParams params) {
                if (fileCallback != null) fileCallback.onReceiveValue(null);
                fileCallback = callback;
                Intent pick = new Intent(Intent.ACTION_GET_CONTENT);
                pick.addCategory(Intent.CATEGORY_OPENABLE);
                pick.setType("image/*");
                try {
                    startActivityForResult(Intent.createChooser(pick, "Выберите фото"), REQ_PHOTO);
                } catch (ActivityNotFoundException e) {
                    fileCallback = null;
                    return false;
                }
                return true;
            }
        });

        // «Назад»: Android 13+ (обязательно для targetSdk 36) — через OnBackInvokedCallback
        if (Build.VERSION.SDK_INT >= 33) {
            getOnBackInvokedDispatcher().registerOnBackInvokedCallback(
                    android.window.OnBackInvokedDispatcher.PRIORITY_DEFAULT, this::handleBack);
        }

        if (savedInstanceState != null) web.restoreState(savedInstanceState);
        else web.loadUrl(START);
    }

    /** Сначала закрываем окна внутри приложения; если закрывать нечего — выходим. */
    private void handleBack() {
        web.evaluateJavascript("(window.appBack && window.appBack()) ? 'y' : 'n'", value -> {
            if (value != null && value.contains("y")) return;
            finish();
        });
    }

    // ── файлы ──
    private File dataFile() { return new File(getFilesDir(), "data.json"); }
    private File photosDir() {
        File d = new File(getFilesDir(), "photos");
        if (!d.exists()) d.mkdirs();
        return d;
    }

    private static byte[] readAll(InputStream in) throws Exception {
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        byte[] buf = new byte[64 * 1024];
        int n;
        while ((n = in.read(buf)) > 0) out.write(buf, 0, n);
        in.close();
        return out.toByteArray();
    }

    /** Фото должно лежать именно в нашей папке photos — защита от чужих путей. */
    private File photoFromUrl(String url) {
        try {
            File f = new File(Uri.parse(url).getPath()).getCanonicalFile();
            if (f.getParentFile() != null && f.getParentFile().equals(photosDir().getCanonicalFile())) return f;
        } catch (Exception ignored) { }
        return null;
    }

    private void callJs(String script) {
        runOnUiThread(() -> web.evaluateJavascript(script, null));
    }

    /** Мост для JavaScript: window.Android.* */
    private class Bridge {
        @JavascriptInterface
        public String loadData() {
            try {
                File f = dataFile();
                if (!f.exists()) return null;
                return new String(readAll(new FileInputStream(f)), StandardCharsets.UTF_8);
            } catch (Exception e) {
                return null;
            }
        }

        @JavascriptInterface
        public boolean saveData(String json) {
            // пишем во временный файл и переименовываем — данные не испортятся при сбое
            try {
                File tmp = new File(getFilesDir(), "data.json.tmp");
                try (FileOutputStream out = new FileOutputStream(tmp)) {
                    out.write(json.getBytes(StandardCharsets.UTF_8));
                    out.getFD().sync();
                }
                return tmp.renameTo(dataFile());
            } catch (Exception e) {
                return false;
            }
        }

        @JavascriptInterface
        public String savePhoto(String base64) {
            try {
                File f = new File(photosDir(), UUID.randomUUID() + ".jpg");
                try (FileOutputStream out = new FileOutputStream(f)) {
                    out.write(Base64.decode(base64, Base64.DEFAULT));
                }
                return Uri.fromFile(f).toString();
            } catch (Exception e) {
                return "";
            }
        }

        @JavascriptInterface
        public String readPhoto(String url) {
            try {
                File f = photoFromUrl(url);
                if (f == null || !f.exists()) return "";
                return Base64.encodeToString(readAll(new FileInputStream(f)), Base64.NO_WRAP);
            } catch (Exception e) {
                return "";
            }
        }

        @JavascriptInterface
        public void deletePhoto(String url) {
            File f = photoFromUrl(url);
            if (f != null) f.delete();
        }

        @JavascriptInterface
        public void exportBackup(String fileName, String json) {
            pendingExport = json;
            runOnUiThread(() -> {
                Intent i = new Intent(Intent.ACTION_CREATE_DOCUMENT);
                i.addCategory(Intent.CATEGORY_OPENABLE);
                i.setType("application/json");
                i.putExtra(Intent.EXTRA_TITLE, fileName);
                try {
                    startActivityForResult(i, REQ_EXPORT);
                } catch (ActivityNotFoundException e) {
                    pendingExport = null;
                    callJs("onBackupSaved(false)");
                }
            });
        }

        @JavascriptInterface
        public void importBackup() {
            runOnUiThread(() -> {
                Intent i = new Intent(Intent.ACTION_OPEN_DOCUMENT);
                i.addCategory(Intent.CATEGORY_OPENABLE);
                i.setType("*/*");
                try {
                    startActivityForResult(i, REQ_IMPORT);
                } catch (ActivityNotFoundException ignored) { }
            });
        }
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        Uri uri = (resultCode == RESULT_OK && data != null) ? data.getData() : null;

        if (requestCode == REQ_PHOTO && fileCallback != null) {
            fileCallback.onReceiveValue(WebChromeClient.FileChooserParams.parseResult(resultCode, data));
            fileCallback = null;
        } else if (requestCode == REQ_EXPORT) {
            String json = pendingExport;
            pendingExport = null;
            if (uri == null || json == null) return;
            boolean ok;
            try (OutputStream out = getContentResolver().openOutputStream(uri, "wt")) {
                out.write(json.getBytes(StandardCharsets.UTF_8));
                ok = true;
            } catch (Exception e) {
                ok = false;
            }
            callJs("onBackupSaved(" + ok + ")");
        } else if (requestCode == REQ_IMPORT && uri != null) {
            try {
                String text = new String(readAll(getContentResolver().openInputStream(uri)), StandardCharsets.UTF_8);
                callJs("onBackupFile(" + JSONObject.quote(text) + ")");
            } catch (Exception e) {
                callJs("toast('Не удалось прочитать файл', true)");
            }
        }
    }

    @Override
    @SuppressWarnings("deprecation")
    public void onBackPressed() {
        handleBack();   // Android 8–12
    }

    @Override
    protected void onSaveInstanceState(Bundle outState) {
        super.onSaveInstanceState(outState);
        web.saveState(outState);
    }
}
