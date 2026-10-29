package tj.ferma.app;

import android.app.Activity;
import android.content.ActivityNotFoundException;
import android.content.Intent;
import android.content.SharedPreferences;
import android.graphics.Color;
import android.net.Uri;
import android.os.Bundle;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.webkit.JavascriptInterface;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.ProgressBar;
import android.widget.TextView;

/**
 * Приложение «Ферма»: показывает интерфейс, который отдаёт сервер бота (/app).
 * Данные общие с Telegram-ботом — та же база.
 */
public class MainActivity extends Activity {

    private static final int FILE_REQUEST = 7;
    private static final String SETUP_PAGE = "file:///android_asset/setup.html";
    private static final String ERROR_PAGE = "file:///android_asset/error.html";

    private WebView web;
    private View loader;
    private SharedPreferences prefs;
    private ValueCallback<Uri[]> fileCallback;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        prefs = getSharedPreferences("ferma", MODE_PRIVATE);

        FrameLayout root = new FrameLayout(this);
        root.setBackgroundColor(Color.parseColor("#F4F1EA"));
        web = new WebView(this);
        web.setBackgroundColor(Color.parseColor("#F4F1EA"));
        root.addView(web, new FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        loader = buildLoader();
        root.addView(loader, new FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        setContentView(root);

        // Отладка WebView только в тестовой (debug) сборке
        if ((getApplicationInfo().flags & android.content.pm.ApplicationInfo.FLAG_DEBUGGABLE) != 0) {
            WebView.setWebContentsDebuggingEnabled(true);
        }

        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setDatabaseEnabled(true);
        s.setLoadWithOverviewMode(true);
        s.setUseWideViewPort(true);
        s.setTextZoom(100);
        s.setMediaPlaybackRequiresUserGesture(false);

        web.addJavascriptInterface(new Bridge(), "Android");

        web.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                Uri url = request.getUrl();
                String server = getServer();
                String scheme = url.getScheme();
                if ("file".equals(scheme)) return false;
                if (!server.isEmpty() && url.toString().startsWith(server)) return false;
                // Внешние ссылки (Telegram и т.п.) открываем в других приложениях
                try { startActivity(new Intent(Intent.ACTION_VIEW, url)); } catch (ActivityNotFoundException ignored) { }
                return true;
            }

            @Override
            public void onPageFinished(WebView view, String url) {
                loader.setVisibility(View.GONE);
            }

            @Override
            public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
                if (request.isForMainFrame()) {
                    loader.setVisibility(View.GONE);
                    view.loadUrl(ERROR_PAGE);
                }
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
                    startActivityForResult(Intent.createChooser(pick, "Выберите фото"), FILE_REQUEST);
                } catch (ActivityNotFoundException e) {
                    fileCallback = null;
                    return false;
                }
                return true;
            }
        });

        if (savedInstanceState != null) {
            web.restoreState(savedInstanceState);
            loader.setVisibility(View.GONE);
        } else {
            openStart();
        }
    }

    private View buildLoader() {
        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);
        box.setGravity(Gravity.CENTER);
        box.setBackgroundColor(Color.parseColor("#F4F1EA"));
        box.setClickable(true);
        TextView icon = new TextView(this);
        icon.setText("🐄");
        icon.setTextSize(56);
        icon.setGravity(Gravity.CENTER);
        box.addView(icon);
        ProgressBar bar = new ProgressBar(this);
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        lp.topMargin = 32;
        box.addView(bar, lp);
        TextView hint = new TextView(this);
        hint.setText("Подключаюсь к серверу…\nЕсли сервер спал, это займёт до минуты");
        hint.setTextColor(Color.parseColor("#6B7468"));
        hint.setTextSize(14);
        hint.setGravity(Gravity.CENTER);
        hint.setPadding(48, 32, 48, 0);
        box.addView(hint);
        return box;
    }

    private String getServer() {
        String s = prefs.getString("server", "");
        if (s.isEmpty()) s = getString(R.string.default_server);
        return normalize(s);
    }

    private static String normalize(String url) {
        String u = url == null ? "" : url.trim();
        if (u.isEmpty()) return "";
        if (!u.startsWith("http://") && !u.startsWith("https://")) u = "https://" + u;
        while (u.endsWith("/")) u = u.substring(0, u.length() - 1);
        if (u.endsWith("/app")) u = u.substring(0, u.length() - 4);
        return u;
    }

    private void openStart() {
        String server = getServer();
        if (server.isEmpty()) {
            loader.setVisibility(View.GONE);
            web.loadUrl(SETUP_PAGE);
        } else {
            loader.setVisibility(View.VISIBLE);
            web.loadUrl(server + "/app");
        }
    }

    /** Мост для JavaScript: window.Android.* */
    private class Bridge {
        @JavascriptInterface
        public void setServer(String url) {
            prefs.edit().putString("server", normalize(url)).apply();
            runOnUiThread(MainActivity.this::openStart);
        }

        @JavascriptInterface
        public void resetServer() {
            runOnUiThread(() -> {
                loader.setVisibility(View.GONE);
                web.loadUrl(SETUP_PAGE);
            });
        }

        @JavascriptInterface
        public String getServer() {
            return MainActivity.this.getServer();
        }

        @JavascriptInterface
        public void retry() {
            runOnUiThread(MainActivity.this::openStart);
        }
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode == FILE_REQUEST && fileCallback != null) {
            fileCallback.onReceiveValue(WebChromeClient.FileChooserParams.parseResult(resultCode, data));
            fileCallback = null;
        }
    }

    @Override
    @SuppressWarnings("deprecation")
    public void onBackPressed() {
        web.evaluateJavascript("(window.appBack && window.appBack()) ? 'y' : 'n'", value -> {
            if (value != null && value.contains("y")) return;
            if (web.canGoBack()) web.goBack();
            else finish();
        });
    }

    @Override
    protected void onSaveInstanceState(Bundle outState) {
        super.onSaveInstanceState(outState);
        web.saveState(outState);
    }
}
