package com.giveawaybot.remote;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.Context;
import android.content.DialogInterface;
import android.content.Intent;
import android.content.SharedPreferences;
import android.graphics.Bitmap;
import android.graphics.Color;
import android.graphics.drawable.ColorDrawable;
import android.net.Uri;
import android.os.Bundle;
import android.text.InputType;
import android.view.Gravity;
import android.view.Menu;
import android.view.MenuItem;
import android.view.View;
import android.view.ViewGroup;
import android.webkit.CookieManager;
import android.webkit.JsResult;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.TextView;

/**
 * Télécommande du Giveaway Bot : affiche l'interface web du serveur (qui tourne sur votre PC)
 * dans une WebView plein écran. Le bot lui-même ne tourne PAS sur le téléphone.
 */
public class MainActivity extends Activity {

    private static final String PREFS = "giveaway_bot";
    private static final String KEY_URL = "server_url";
    private static final int MENU_RELOAD = 1;
    private static final int MENU_ADDRESS = 2;
    private static final int MENU_BROWSER = 3;

    private WebView webView;
    private LinearLayout errorBox;
    private TextView errorText;
    private boolean loadFailed = false;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        int dark = Color.parseColor("#0A0A0A");
        getWindow().setStatusBarColor(dark);
        getWindow().setNavigationBarColor(dark);
        if (getActionBar() != null) {
            getActionBar().setBackgroundDrawable(new ColorDrawable(Color.parseColor("#141414")));
            getActionBar().setTitle("Giveaway Bot");
        }

        FrameLayout root = new FrameLayout(this);
        root.setBackgroundColor(dark);

        webView = new WebView(this);
        webView.setBackgroundColor(dark);
        root.addView(webView, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));

        errorBox = new LinearLayout(this);
        errorBox.setOrientation(LinearLayout.VERTICAL);
        errorBox.setGravity(Gravity.CENTER);
        errorBox.setBackgroundColor(dark);
        errorBox.setPadding(dp(24), dp(24), dp(24), dp(24));
        errorText = new TextView(this);
        errorText.setTextColor(Color.parseColor("#E5E5E5"));
        errorText.setTextSize(16);
        errorText.setGravity(Gravity.CENTER);
        errorBox.addView(errorText);
        Button retry = new Button(this);
        retry.setText("Réessayer");
        retry.setOnClickListener(v -> loadServer());
        errorBox.addView(retry);
        Button change = new Button(this);
        change.setText("Modifier l'adresse du serveur");
        change.setOnClickListener(v -> askAddress(false));
        errorBox.addView(change);
        errorBox.setVisibility(View.GONE);
        root.addView(errorBox, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        setContentView(root);

        WebSettings s = webView.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setLoadWithOverviewMode(false);
        s.setTextZoom(100);
        CookieManager cm = CookieManager.getInstance();
        cm.setAcceptCookie(true);   // le cookie de session (mot de passe) est conservé

        webView.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                Uri target = request.getUrl();
                Uri base = Uri.parse(serverUrl());
                if (base.getHost() != null && base.getHost().equals(target.getHost())) {
                    return false;                                   // même serveur : reste dans l'application
                }
                startActivity(new Intent(Intent.ACTION_VIEW, target)); // liens Instagram, etc. : navigateur
                return true;
            }

            @Override
            public void onPageStarted(WebView view, String url, Bitmap favicon) {
                loadFailed = false;
            }

            @Override
            public void onPageFinished(WebView view, String url) {
                if (!loadFailed) errorBox.setVisibility(View.GONE);
            }

            @Override
            public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
                if (request.isForMainFrame()) {
                    loadFailed = true;
                    showError("Impossible de joindre le serveur\n" + serverUrl() + "\n\n"
                            + "Vérifiez que le PC est allumé et que run-mobile.bat tourne.\n\n"
                            + "Téléphone en 4G ou sur un autre réseau que le PC : il faut Tailscale (gratuit) sur le PC ET sur le téléphone, "
                            + "puis l'adresse 100.x.x.x:8000.");
                }
            }
        });

        // Sans ça, les confirm()/alert() de l'interface (ex. « Désactiver le Dry-Run ? ») sont ignorés par la WebView.
        webView.setWebChromeClient(new WebChromeClient() {
            @Override
            public boolean onJsAlert(WebView view, String url, String message, final JsResult result) {
                new AlertDialog.Builder(MainActivity.this).setMessage(message)
                        .setPositiveButton("OK", (d, w) -> result.confirm())
                        .setOnCancelListener(d -> result.cancel()).show();
                return true;
            }

            @Override
            public boolean onJsConfirm(WebView view, String url, String message, final JsResult result) {
                new AlertDialog.Builder(MainActivity.this).setMessage(message)
                        .setPositiveButton("OK", (d, w) -> result.confirm())
                        .setNegativeButton("Annuler", (d, w) -> result.cancel())
                        .setOnCancelListener(d -> result.cancel()).show();
                return true;
            }
        });

        if (prefs().getString(KEY_URL, "").isEmpty()) {
            askAddress(true);
        } else {
            loadServer();
        }
    }

    // ---- adresse du serveur -------------------------------------------------------------------
    private SharedPreferences prefs() {
        return getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }

    private String serverUrl() {
        return prefs().getString(KEY_URL, "");
    }

    private static String normalize(String raw) {
        String u = raw.trim();
        if (u.isEmpty()) return "";
        if (!u.startsWith("http://") && !u.startsWith("https://")) u = "http://" + u;
        while (u.endsWith("/")) u = u.substring(0, u.length() - 1);
        // port par défaut du bot si l'utilisateur n'a saisi que l'adresse IP
        String hostPart = u.replaceFirst("^https?://", "");
        if (u.startsWith("http://") && !hostPart.contains(":") && !hostPart.contains("/")) u = u + ":8000";
        return u;
    }

    private void askAddress(final boolean firstRun) {
        final EditText input = new EditText(this);
        input.setHint("192.168.1.20:8000");
        input.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI);
        input.setText(serverUrl());
        input.setSelection(input.getText().length());
        FrameLayout box = new FrameLayout(this);
        box.setPadding(dp(20), dp(8), dp(20), 0);
        box.addView(input);
        AlertDialog.Builder b = new AlertDialog.Builder(this)
                .setTitle("Adresse du serveur")
                .setMessage("Entrez l'adresse affichée par run-mobile.bat sur votre PC (ex : 192.168.1.20:8000).")
                .setView(box)
                .setPositiveButton("Connecter", (d, w) -> {
                    String url = normalize(input.getText().toString());
                    if (isUnusable(url)) {
                        showBadAddress();
                    } else if (!url.isEmpty()) {
                        prefs().edit().putString(KEY_URL, url).apply();
                        loadServer();
                    } else if (firstRun) {
                        askAddress(true);
                    }
                });
        if (!firstRun) b.setNegativeButton("Annuler", null);
        else b.setCancelable(false);
        b.show();
    }

    /** 0.0.0.0 est le mode d'écoute du serveur, 127.0.0.1/localhost désignent le téléphone lui-même. */
    private static boolean isUnusable(String url) {
        String host = Uri.parse(url.isEmpty() ? "http://x" : url).getHost();
        return host != null && (host.equals("0.0.0.0") || host.equals("127.0.0.1") || host.equals("localhost"));
    }

    private void showBadAddress() {
        new AlertDialog.Builder(this)
                .setTitle("Adresse inutilisable")
                .setMessage("0.0.0.0, 127.0.0.1 et localhost ne fonctionnent pas depuis le téléphone.\n\n"
                        + "Utilisez l'adresse affichée par run-mobile.bat :\n"
                        + "• 100.x.x.x:8000 (Tailscale) si le téléphone est en 4G ou sur un autre réseau que le PC\n"
                        + "• 192.168.x.x:8000 seulement si le téléphone est sur le même Wi-Fi que le PC")
                .setPositiveButton("Corriger", (d, w) -> askAddress(false))
                .show();
    }

    private void loadServer() {
        String url = serverUrl();
        if (url.isEmpty()) {
            askAddress(true);
            return;
        }
        if (isUnusable(url)) {          // adresse invalide déjà enregistrée par une version précédente
            showBadAddress();
            return;
        }
        loadFailed = false;
        errorBox.setVisibility(View.GONE);
        webView.loadUrl(url);
    }

    private void showError(String message) {
        errorText.setText(message);
        errorBox.setVisibility(View.VISIBLE);
    }

    private int dp(int v) {
        return Math.round(v * getResources().getDisplayMetrics().density);
    }

    // ---- menu / navigation --------------------------------------------------------------------
    @Override
    public boolean onCreateOptionsMenu(Menu menu) {
        menu.add(0, MENU_RELOAD, 0, "Actualiser");
        menu.add(0, MENU_ADDRESS, 1, "Changer l'adresse du serveur");
        menu.add(0, MENU_BROWSER, 2, "Ouvrir dans le navigateur");
        return true;
    }

    @Override
    public boolean onOptionsItemSelected(MenuItem item) {
        switch (item.getItemId()) {
            case MENU_RELOAD:
                loadServer();
                return true;
            case MENU_ADDRESS:
                askAddress(false);
                return true;
            case MENU_BROWSER:
                if (!serverUrl().isEmpty()) startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse(serverUrl())));
                return true;
            default:
                return super.onOptionsItemSelected(item);
        }
    }

    @Override
    public void onBackPressed() {
        if (webView.canGoBack()) webView.goBack();
        else super.onBackPressed();
    }

    @Override
    protected void onPause() {
        super.onPause();
        CookieManager.getInstance().flush();   // écrit le cookie de session sur le disque
    }
}
