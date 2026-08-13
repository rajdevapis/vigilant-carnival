from flask import Flask, request, jsonify, render_template, Response
import os, re, json, base64, uuid, urllib.request, urllib.error, io, zipfile
from PIL import Image

app = Flask(__name__)

@app.errorhandler(Exception)
def handle_any_error(e):
    import traceback
    code = getattr(e, "code", 500)
    if not isinstance(code, int): code = 500
    traceback.print_exc()
    return jsonify({"success": False, "error": f"Server error: {e}"}), code

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_OWNER = os.environ.get("GITHUB_OWNER", "")
GITHUB_REPO  = os.environ.get("GITHUB_REPO", "apk-builds")

AD_SESSIONS = {}

PKG_RE = re.compile(r'^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z][A-Za-z0-9_]*)+$')

def gh(method, path, body=None):
    url  = f"https://api.github.com{path}"
    data = json.dumps(body).encode() if body else None
    req  = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"token {GITHUB_TOKEN}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return json.loads(r.read()), r.status
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read()), e.code
        except Exception:
            return {"message": f"GitHub returned status {e.code}"}, e.code
    except Exception as e:
        return {"message": f"GitHub error: {e}"}, 502

def b64(t): return base64.b64encode(t.encode()).decode()
def sanitize(n): return re.sub(r'[^a-zA-Z0-9_\-]', '_', n)
def valid_pkg(p): return bool(p and PKG_RE.match(p))

def create_blob(content_bytes):
    body = {"content": base64.b64encode(content_bytes).decode(), "encoding": "base64"}
    resp, status = gh("POST", f"/repos/{GITHUB_OWNER}/{GITHUB_REPO}/git/blobs", body)
    if status not in (200, 201): return None
    return resp["sha"]

ICON_SIZES = {"mipmap-mdpi": 48, "mipmap-hdpi": 72, "mipmap-xhdpi": 96,
              "mipmap-xxhdpi": 144, "mipmap-xxxhdpi": 192}

def fetch_icon_bytes(icon_url, fallback_site_url=None):
    candidates = []
    if icon_url:
        candidates.append(icon_url)
    elif fallback_site_url:
        m = re.match(r'^(https?://[^/]+)', fallback_site_url)
        if m:
            candidates.append(m.group(1) + "/favicon.ico")
    for url in candidates:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.read()
        except Exception:
            continue
    return None

def build_icon_files(icon_bytes):
    try:
        img = Image.open(io.BytesIO(icon_bytes)).convert("RGBA")
    except Exception:
        return None
    files = {}
    for folder, size in ICON_SIZES.items():
        resized = img.resize((size, size), Image.LANCZOS)
        buf = io.BytesIO()
        resized.save(buf, format="PNG")
        png_bytes = buf.getvalue()
        files[f"app/src/main/res/{folder}/ic_launcher.png"] = png_bytes
        files[f"app/src/main/res/{folder}/ic_launcher_round.png"] = png_bytes
    return files

# ─── ANDROID FILE GENERATORS ────────────────────────────────────────────

def make_main_activity_unity(pkg, unity_id, content_type, content_value,
                              has_rewarded, has_interstitial, auto_show, test_mode,
                              entry_file="index.html", permissions=None):
    if permissions is None: permissions = []
    # Generate permission request code
    perm_request_code = 123
    perm_list = [p for p in permissions if p.startswith("android.permission.")]
    perm_vars = ",\n        ".join(perm_list)
    request_perms_code = ""
    if perm_list:
        request_perms_code = f"""
    private static final int PERMISSION_REQUEST_CODE = {perm_request_code};
    private void checkAndRequestPermissions() {{
        List<String> needed = new ArrayList<>();
        for (String perm : new String[]{{{perm_vars}}}) {{
            if (checkSelfPermission(perm) != PackageManager.PERMISSION_GRANTED) {{
                needed.add(perm);
            }}
        }}
        if (!needed.isEmpty()) {{
            requestPermissions(needed.toArray(new String[0]), PERMISSION_REQUEST_CODE);
        }}
    }}
    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] grantResults) {{
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
        if (requestCode == PERMISSION_REQUEST_CODE) {{
            // simply proceed
        }}
    }}
"""
    else:
        request_perms_code = ""

    if content_type == "url":
        load_line = f'webView.loadUrl("{content_value}");'
    elif content_type == "zip":
        load_line = f'webView.loadUrl("file:///android_asset/{entry_file}");'
    else:  # html
        esc = (content_value.replace('\\','\\\\').replace('"','\\"')
                            .replace('\n','\\n').replace('\r',''))
        load_line = f'webView.loadDataWithBaseURL(null, "{esc}", "text/html", "UTF-8", null);'

    r_place = '"Rewarded_Android"'     if has_rewarded     else 'null'
    i_place = '"Interstitial_Android"' if has_interstitial else 'null'
    r_vis   = "View.VISIBLE" if (has_rewarded     and auto_show) else "View.GONE"
    i_vis   = "View.VISIBLE" if (has_interstitial and auto_show) else "View.GONE"
    auto_j  = "true" if auto_show  else "false"
    test_j  = "true" if test_mode  else "false"

    return f"""package {pkg};

import android.app.Activity;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.util.Log;
import android.view.View;
import android.webkit.JavascriptInterface;
import android.webkit.WebChromeClient;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.Toast;
import com.unity3d.ads.IUnityAdsInitializationListener;
import com.unity3d.ads.IUnityAdsLoadListener;
import com.unity3d.ads.IUnityAdsShowListener;
import com.unity3d.ads.UnityAds;
import com.unity3d.ads.UnityAdsShowOptions;
import android.content.pm.PackageManager;
import android.Manifest;
import java.util.ArrayList;
import java.util.List;

public class MainActivity extends Activity implements IUnityAdsInitializationListener {{

    private static final String TAG              = "UnityAdsApp";
    private static final String UNITY_GAME_ID   = "{unity_id}";
    private static final boolean TEST_MODE       = {test_j};
    private static final String REWARDED         = {r_place};
    private static final String INTERSTITIAL     = {i_place};
    private static final boolean AUTO_SHOW       = {auto_j};
    private static final int RETRY_MS            = 2000;
    private static final int FAST_RETRY_MS       = 600;

    private WebView webView;
    private Button  btnRewarded, btnInterstitial;
    private View    loadingOverlay, errorOverlay;
    private boolean rewardedReady = false, interstitialReady = false, sdkReady = false;
    private boolean loadingR = false, loadingI = false;
    private boolean pendingR = false, pendingI = false;
    private boolean autoFired = false;
    private int     autoAttempts = 0;
    private final Handler handler = new Handler(Looper.getMainLooper());

    {request_perms_code}

    @Override
    protected void onCreate(Bundle savedInstanceState) {{
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);

        webView         = findViewById(R.id.webView);
        btnRewarded     = findViewById(R.id.btnRewarded);
        btnInterstitial = findViewById(R.id.btnInterstitial);
        loadingOverlay  = findViewById(R.id.loadingOverlay);
        errorOverlay    = findViewById(R.id.errorOverlay);
        Button btnRetry = findViewById(R.id.btnRetry);
        btnRetry.setOnClickListener(v -> {{
            errorOverlay.setVisibility(View.GONE);
            loadingOverlay.setVisibility(View.VISIBLE);
            webView.reload();
        }});

        WebSettings ws = webView.getSettings();
        ws.setJavaScriptEnabled(true);
        ws.setDomStorageEnabled(true);
        ws.setLoadWithOverviewMode(true);
        ws.setUseWideViewPort(true);
        ws.setCacheMode(WebSettings.LOAD_DEFAULT);
        ws.setSupportMultipleWindows(true);
        ws.setJavaScriptCanOpenWindowsAutomatically(true);

        webView.addJavascriptInterface(new AdBridge(), "NativeAds");

        webView.setWebViewClient(new WebViewClient() {{
            @Override
            public boolean shouldOverrideUrlLoading(WebView v, String url) {{
                if (url.startsWith("ads://show_rewarded"))    {{ triggerRewarded();     return true; }}
                if (url.startsWith("ads://show_interstitial")){{ triggerInterstitial(); return true; }}
                return false;
            }}
            @Override
            public void onPageStarted(WebView v, String url, android.graphics.Bitmap favicon) {{
                errorOverlay.setVisibility(View.GONE);
                loadingOverlay.setVisibility(View.VISIBLE);
            }}
            @Override
            public void onPageFinished(WebView v, String url) {{
                loadingOverlay.setVisibility(View.GONE);
            }}
            @Override
            public void onReceivedError(WebView v, android.webkit.WebResourceRequest req,
                    android.webkit.WebResourceError err) {{
                if (req.isForMainFrame()) {{
                    loadingOverlay.setVisibility(View.GONE);
                    errorOverlay.setVisibility(View.VISIBLE);
                }}
            }}
        }});

        webView.setWebChromeClient(new WebChromeClient() {{
            @Override
            public boolean onCreateWindow(WebView view, boolean isDialog,
                    boolean isUserGesture, android.os.Message resultMsg) {{
                WebView.HitTestResult r = view.getHitTestResult();
                String u = r != null ? r.getExtra() : null;
                if (u != null) view.loadUrl(u);
                return false;
            }}
        }});

        {load_line}

        btnRewarded.setVisibility({r_vis});
        btnInterstitial.setVisibility({i_vis});

        // Request permissions if any
        checkAndRequestPermissions();

        UnityAds.initialize(this, UNITY_GAME_ID, TEST_MODE, this);

        handler.postDelayed(() -> {{
            if (!sdkReady) Toast.makeText(this,
                "Ads SDK ready nahi — Game ID/internet check karo", Toast.LENGTH_LONG).show();
        }}, 12000);

        btnRewarded.setOnClickListener(v -> {{
            if (!sdkReady) {{ Toast.makeText(this,"SDK init ho raha...",Toast.LENGTH_SHORT).show(); return; }}
            if (rewardedReady) showRewarded();
            else {{ pendingR = true; Toast.makeText(this,"Loading, abhi aata hai...",Toast.LENGTH_SHORT).show(); loadRewarded(); }}
        }});

        btnInterstitial.setOnClickListener(v -> {{
            if (!sdkReady) {{ Toast.makeText(this,"SDK init ho raha...",Toast.LENGTH_SHORT).show(); return; }}
            if (interstitialReady) showInterstitial();
            else {{ pendingI = true; Toast.makeText(this,"Loading, abhi aata hai...",Toast.LENGTH_SHORT).show(); loadInterstitial(); }}
        }});
    }}

    @Override
    public void onInitializationComplete() {{
        sdkReady = true;
        if (REWARDED != null)     loadRewarded();
        if (INTERSTITIAL != null) loadInterstitial();
        if (AUTO_SHOW) maybeAutoShow();
    }}

    @Override
    public void onInitializationFailed(UnityAds.UnityAdsInitializationError e, String m) {{
        Log.e(TAG, "Init failed: " + e + " " + m);
        handler.postDelayed(() -> UnityAds.initialize(this, UNITY_GAME_ID, TEST_MODE, this), RETRY_MS);
    }}

    private void maybeAutoShow() {{
        if (autoFired || autoAttempts++ > 20) return;
        boolean rOk = REWARDED == null     || rewardedReady;
        boolean iOk = INTERSTITIAL == null || interstitialReady;
        if (!rOk || !iOk) {{ handler.postDelayed(this::maybeAutoShow, 800); return; }}
        autoFired = true;
        if (REWARDED != null) {{
            showRewarded();
            if (INTERSTITIAL != null) handler.postDelayed(this::showInterstitial, 4000);
        }} else if (INTERSTITIAL != null) showInterstitial();
    }}

    private void triggerRewarded()     {{ runOnUiThread(() -> {{ if (rewardedReady)     showRewarded();     else {{ pendingR = true; loadRewarded(); }} }}); }}
    private void triggerInterstitial() {{ runOnUiThread(() -> {{ if (interstitialReady) showInterstitial(); else {{ pendingI = true; loadInterstitial(); }} }}); }}

    private class AdBridge {{
        @JavascriptInterface public void showRewarded()     {{ triggerRewarded(); }}
        @JavascriptInterface public void showInterstitial() {{ triggerInterstitial(); }}
    }}

    private void loadRewarded() {{
        if (REWARDED == null || !sdkReady || loadingR) return;
        loadingR = true;
        UnityAds.load(REWARDED, new IUnityAdsLoadListener() {{
            public void onUnityAdsAdLoaded(String p)    {{ loadingR=false; rewardedReady=true; if(pendingR){{pendingR=false;showRewarded();}} }}
            public void onUnityAdsFailedToLoad(String p, UnityAds.UnityAdsLoadError e, String m) {{
                loadingR=false; rewardedReady=false;
                handler.postDelayed(()->loadRewarded(), pendingR?FAST_RETRY_MS:RETRY_MS);
            }}
        }});
    }}

    private void loadInterstitial() {{
        if (INTERSTITIAL == null || !sdkReady || loadingI) return;
        loadingI = true;
        UnityAds.load(INTERSTITIAL, new IUnityAdsLoadListener() {{
            public void onUnityAdsAdLoaded(String p)    {{ loadingI=false; interstitialReady=true; if(pendingI){{pendingI=false;showInterstitial();}} }}
            public void onUnityAdsFailedToLoad(String p, UnityAds.UnityAdsLoadError e, String m) {{
                loadingI=false; interstitialReady=false;
                handler.postDelayed(()->loadInterstitial(), pendingI?FAST_RETRY_MS:RETRY_MS);
            }}
        }});
    }}

    private void showRewarded() {{
        rewardedReady = false;
        UnityAds.show(this, REWARDED, new UnityAdsShowOptions(), new IUnityAdsShowListener() {{
            public void onUnityAdsShowFailure(String p,UnityAds.UnityAdsShowError e,String m) {{ loadRewarded(); }}
            public void onUnityAdsShowStart(String p)  {{}}
            public void onUnityAdsShowClick(String p)  {{}}
            public void onUnityAdsShowComplete(String p,UnityAds.UnityAdsShowCompletionState s) {{
                if(s==UnityAds.UnityAdsShowCompletionState.COMPLETED)
                    Toast.makeText(MainActivity.this,"🎁 Reward mila!",Toast.LENGTH_SHORT).show();
                loadRewarded();
            }}
        }});
    }}

    private void showInterstitial() {{
        interstitialReady = false;
        UnityAds.show(this, INTERSTITIAL, new UnityAdsShowOptions(), new IUnityAdsShowListener() {{
            public void onUnityAdsShowFailure(String p,UnityAds.UnityAdsShowError e,String m) {{ loadInterstitial(); }}
            public void onUnityAdsShowStart(String p)  {{}}
            public void onUnityAdsShowClick(String p)  {{}}
            public void onUnityAdsShowComplete(String p,UnityAds.UnityAdsShowCompletionState s) {{ loadInterstitial(); }}
        }});
    }}

    @Override protected void onResume() {{
        super.onResume();
        if (sdkReady) {{ if(!rewardedReady) loadRewarded(); if(!interstitialReady) loadInterstitial(); }}
    }}
    @Override protected void onDestroy() {{ handler.removeCallbacksAndMessages(null); super.onDestroy(); }}
    @Override public void onBackPressed() {{ if(webView.canGoBack()) webView.goBack(); else super.onBackPressed(); }}
}}
"""

def make_main_activity_startapp(pkg, startapp_id, content_type, content_value,
                                 has_banner, has_interstitial, has_rewarded, auto_show,
                                 test_mode=False, rewarded_placement="", entry_file="index.html",
                                 permissions=None):
    if permissions is None: permissions = []
    # Generate permission request code
    perm_request_code = 123
    perm_list = [p for p in permissions if p.startswith("android.permission.")]
    perm_vars = ",\n        ".join(perm_list)
    request_perms_code = ""
    if perm_list:
        request_perms_code = f"""
    private static final int PERMISSION_REQUEST_CODE = {perm_request_code};
    private void checkAndRequestPermissions() {{
        List<String> needed = new ArrayList<>();
        for (String perm : new String[]{{{perm_vars}}}) {{
            if (checkSelfPermission(perm) != PackageManager.PERMISSION_GRANTED) {{
                needed.add(perm);
            }}
        }}
        if (!needed.isEmpty()) {{
            requestPermissions(needed.toArray(new String[0]), PERMISSION_REQUEST_CODE);
        }}
    }}
    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] grantResults) {{
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
        if (requestCode == PERMISSION_REQUEST_CODE) {{
            // simply proceed
        }}
    }}
"""
    else:
        request_perms_code = ""

    if content_type == "url":
        load_line = f'webView.loadUrl("{content_value}");'
    elif content_type == "zip":
        load_line = f'webView.loadUrl("file:///android_asset/{entry_file}");'
    else:
        esc = (content_value.replace('\\','\\\\').replace('"','\\"')
                            .replace('\n','\\n').replace('\r',''))
        load_line = f'webView.loadDataWithBaseURL(null, "{esc}", "text/html", "UTF-8", null);'

    test_j = "true" if test_mode else "false"

    banner_code = ""
    if has_banner:
        banner_code = """
        // StartApp Banner
        Banner startAppBanner = new Banner(this);
        bannerLayout.addView(startAppBanner);"""

    interstitial_code = ""
    if has_interstitial:
        interstitial_code = """
        // StartApp Interstitial preload
        interstitialAd = new StartAppAd(this);
        interstitialAd.loadAd();"""

    rewarded_code = ""
    if has_rewarded:
        rewarded_code = """
        // StartApp Rewarded Video preload
        loadRewarded();"""

    auto_code = ""
    if auto_show and has_interstitial:
        auto_code = """
        // Auto show on open
        handler.postDelayed(() -> {
            if (interstitialAd != null) interstitialAd.showAd();
        }, 2000);"""

    r_vis = "View.VISIBLE" if (has_rewarded     and auto_show) else "View.GONE"
    i_vis = "View.VISIBLE" if (has_interstitial and auto_show) else "View.GONE"

    placement_param = f'"{rewarded_placement}"' if rewarded_placement else "null"
    load_method = f'rewardedAd.loadAd(StartAppAd.AdMode.REWARDED_VIDEO, {placement_param}, listener);' if rewarded_placement else 'rewardedAd.loadAd(StartAppAd.AdMode.REWARDED_VIDEO, listener);'

    return f"""package {pkg};

import android.app.Activity;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.view.View;
import android.webkit.JavascriptInterface;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.FrameLayout;
import android.widget.Toast;
import android.util.Log;
import com.startapp.sdk.adsbase.Ad;
import com.startapp.sdk.adsbase.StartAppAd;
import com.startapp.sdk.adsbase.StartAppSDK;
import com.startapp.sdk.adsbase.adlisteners.AdEventListener;
import com.startapp.sdk.adsbase.adlisteners.VideoListener;
import com.startapp.sdk.ads.banner.Banner;
import android.content.pm.PackageManager;
import android.Manifest;
import java.util.ArrayList;
import java.util.List;

public class MainActivity extends Activity {{

    private static final String APP_ID    = "{startapp_id}";
    private static final boolean TEST_MODE = {test_j};
    private static final String TAG    = "StartAppAds";
    private static final int RETRY_MS  = 2000;
    private static final int FAST_RETRY_MS = 600;

    private WebView webView;
    private FrameLayout bannerLayout;
    private Button btnRewarded, btnInterstitial;
    private View loadingOverlay, errorOverlay;
    private StartAppAd interstitialAd;
    private StartAppAd rewardedAd;
    private final Handler handler = new Handler(Looper.getMainLooper());

    private boolean loadingRewarded = false;
    private boolean pendingRewarded = false;

    {request_perms_code}

    @Override
    protected void onCreate(Bundle savedInstanceState) {{
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main_startapp);

        webView         = findViewById(R.id.webView);
        bannerLayout    = findViewById(R.id.bannerLayout);
        btnRewarded     = findViewById(R.id.btnRewarded);
        btnInterstitial = findViewById(R.id.btnInterstitial);
        loadingOverlay  = findViewById(R.id.loadingOverlay);
        errorOverlay    = findViewById(R.id.errorOverlay);
        Button btnRetry = findViewById(R.id.btnRetry);
        btnRetry.setOnClickListener(v -> {{
            errorOverlay.setVisibility(View.GONE);
            loadingOverlay.setVisibility(View.VISIBLE);
            webView.reload();
        }});

        WebSettings ws = webView.getSettings();
        ws.setJavaScriptEnabled(true);
        ws.setDomStorageEnabled(true);
        ws.setLoadWithOverviewMode(true);
        ws.setUseWideViewPort(true);

        webView.addJavascriptInterface(new AdBridge(), "NativeAds");

        webView.setWebViewClient(new WebViewClient() {{
            @Override
            public boolean shouldOverrideUrlLoading(WebView v, String url) {{
                if (url.startsWith("ads://show_rewarded"))     {{ showRewarded();     return true; }}
                if (url.startsWith("ads://show_interstitial")) {{ showInterstitial(); return true; }}
                return false;
            }}
            @Override
            public void onPageStarted(WebView v, String url, android.graphics.Bitmap favicon) {{
                errorOverlay.setVisibility(View.GONE);
                loadingOverlay.setVisibility(View.VISIBLE);
            }}
            @Override
            public void onPageFinished(WebView v, String url) {{
                loadingOverlay.setVisibility(View.GONE);
            }}
            @Override
            public void onReceivedError(WebView v, android.webkit.WebResourceRequest req,
                    android.webkit.WebResourceError err) {{
                if (req.isForMainFrame()) {{
                    loadingOverlay.setVisibility(View.GONE);
                    errorOverlay.setVisibility(View.VISIBLE);
                }}
            }}
        }});
        {load_line}

        StartAppSDK.setTestAdsEnabled(TEST_MODE);
        StartAppSDK.init(this, APP_ID, false);
        {banner_code}
        {interstitial_code}
        {rewarded_code}
        {auto_code}

        if (btnRewarded != null) {{
            btnRewarded.setVisibility({r_vis});
            btnRewarded.setOnClickListener(v -> showRewarded());
        }}
        if (btnInterstitial != null) {{
            btnInterstitial.setVisibility({i_vis});
            btnInterstitial.setOnClickListener(v -> showInterstitial());
        }}

        // Request permissions if any
        checkAndRequestPermissions();
    }}

    private void showInterstitial() {{
        if (interstitialAd != null && interstitialAd.isReady()) {{
            interstitialAd.showAd();
            interstitialAd.loadAd();
        }} else {{
            Toast.makeText(this, "Ad load ho raha hai, ek pal ruko...", Toast.LENGTH_SHORT).show();
        }}
    }}

    private void loadRewarded() {{
        if (loadingRewarded) return;
        loadingRewarded = true;
        if (rewardedAd == null) {{
            rewardedAd = new StartAppAd(this);
            rewardedAd.setVideoListener(new VideoListener() {{
                @Override public void onVideoCompleted() {{
                    runOnUiThread(() -> Toast.makeText(MainActivity.this,
                        "🎁 Reward mila!", Toast.LENGTH_SHORT).show());
                }}
            }});
        }}
        AdEventListener listener = new AdEventListener() {{
            @Override public void onReceiveAd(Ad ad) {{
                loadingRewarded = false;
                Log.d(TAG, "Rewarded ad ready");
                if (pendingRewarded) {{
                    pendingRewarded = false;
                    showRewarded();
                }}
            }}
            @Override public void onFailedToReceiveAd(Ad ad) {{
                loadingRewarded = false;
                Log.e(TAG, "Rewarded ad load FAILED — no fill ya App ID galat ho sakta hai");
                handler.postDelayed(() -> loadRewarded(), pendingRewarded ? FAST_RETRY_MS : RETRY_MS);
            }}
        }};
        {load_method}
    }}

    private void showRewarded() {{
        if (rewardedAd != null && rewardedAd.isReady()) {{
            rewardedAd.showAd();
            handler.postDelayed(() -> loadRewarded(), 1000);
        }} else {{
            Toast.makeText(this, "Ad load ho raha hai, ek pal ruko...", Toast.LENGTH_SHORT).show();
            pendingRewarded = true;
            loadRewarded();
        }}
    }}

    private class AdBridge {{
        @JavascriptInterface public void showRewarded()     {{ runOnUiThread(MainActivity.this::showRewarded); }}
        @JavascriptInterface public void showInterstitial() {{ runOnUiThread(MainActivity.this::showInterstitial); }}
    }}

    @Override protected void onResume() {{
        super.onResume();
        if (interstitialAd != null) interstitialAd.loadAd();
        if (rewardedAd != null && !rewardedAd.isReady()) loadRewarded();
    }}
    @Override protected void onDestroy() {{
        handler.removeCallbacksAndMessages(null);
        super.onDestroy();
    }}
    @Override public void onBackPressed() {{
        if (webView.canGoBack()) webView.goBack(); else super.onBackPressed();
    }}
}}
"""

def make_manifest(pkg, app_name, network="unity", has_icon=False, permissions=None):
    if permissions is None: permissions = []
    # Always add INTERNET and ACCESS_NETWORK_STATE (required for ads)
    all_perms = set(permissions)
    all_perms.add("android.permission.INTERNET")
    all_perms.add("android.permission.ACCESS_NETWORK_STATE")
    perm_xml = "\n".join(f'    <uses-permission android:name="{p}" />' for p in all_perms if p)
    icon_attrs = ' android:icon="@mipmap/ic_launcher" android:roundIcon="@mipmap/ic_launcher_round"' if has_icon else ""
    unity_activities = ""
    if network == "unity":
        unity_activities = """
        <activity android:name="com.unity3d.ads.adunit.AdUnitActivity"
            android:configChanges="fontScale|keyboard|keyboardHidden|locale|mnc|mcc|navigation|orientation|screenLayout|screenSize|smallestScreenSize|uiMode|touchscreen"
            android:hardwareAccelerated="true"
            android:theme="@android:style/Theme.NoTitleBar.Fullscreen"/>
        <activity android:name="com.unity3d.ads.adunit.AdUnitTransparentActivity"
            android:configChanges="fontScale|keyboard|keyboardHidden|locale|mnc|mcc|navigation|orientation|screenLayout|screenSize|smallestScreenSize|uiMode|touchscreen"
            android:hardwareAccelerated="true"
            android:theme="@android:style/Theme.Translucent.NoTitleBar.Fullscreen"/>"""
    return f"""<?xml version="1.0" encoding="utf-8"?>
<manifest xmlns:android="http://schemas.android.com/apk/res/android">
{perm_xml}
    <application android:label="{app_name}" android:allowBackup="true"
        android:usesCleartextTraffic="true"{icon_attrs}
        android:theme="@style/Theme.AppCompat.NoActionBar">
        <activity android:name=".MainActivity" android:exported="true"
            android:configChanges="keyboard|keyboardHidden|orientation|screenSize">
            <intent-filter>
                <action android:name="android.intent.action.MAIN"/>
                <category android:name="android.intent.category.LAUNCHER"/>
            </intent-filter>
        </activity>{unity_activities}
    </application>
</manifest>
"""

LOADING_OVERLAY_XML = """
    <FrameLayout android:id="@+id/loadingOverlay"
        android:layout_width="match_parent" android:layout_height="match_parent"
        android:background="#121212" android:clickable="true" android:focusable="true">
        <LinearLayout android:layout_width="wrap_content" android:layout_height="wrap_content"
            android:layout_gravity="center" android:orientation="vertical" android:gravity="center">
            <TextView android:text="@string/app_name" android:textColor="#FFFFFF"
                android:textSize="18sp" android:textStyle="bold" android:layout_marginBottom="20dp"
                android:layout_width="wrap_content" android:layout_height="wrap_content"/>
            <ProgressBar android:layout_width="wrap_content" android:layout_height="wrap_content"/>
        </LinearLayout>
    </FrameLayout>
    <FrameLayout android:id="@+id/errorOverlay" android:visibility="gone"
        android:layout_width="match_parent" android:layout_height="match_parent"
        android:background="#121212" android:clickable="true" android:focusable="true">
        <LinearLayout android:layout_width="wrap_content" android:layout_height="wrap_content"
            android:layout_gravity="center" android:orientation="vertical" android:gravity="center"
            android:padding="24dp">
            <TextView android:text="⚠️ Load nahi ho paya" android:textColor="#FFFFFF"
                android:textSize="18sp" android:textStyle="bold" android:layout_marginBottom="8dp"
                android:layout_width="wrap_content" android:layout_height="wrap_content"/>
            <TextView android:text="Internet check karo aur dobara try karo"
                android:textColor="#AAAAAA" android:textSize="14sp" android:layout_marginBottom="20dp"
                android:gravity="center"
                android:layout_width="wrap_content" android:layout_height="wrap_content"/>
            <Button android:id="@+id/btnRetry" android:text="Try Again"
                android:textColor="#FFFFFF" android:backgroundTint="#4CAF50"
                android:layout_width="wrap_content" android:layout_height="wrap_content"/>
        </LinearLayout>
    </FrameLayout>"""

def make_layout_unity():
    return f"""<?xml version="1.0" encoding="utf-8"?>
<FrameLayout xmlns:android="http://schemas.android.com/apk/res/android"
    android:layout_width="match_parent" android:layout_height="match_parent">
    <WebView android:id="@+id/webView"
        android:layout_width="match_parent" android:layout_height="match_parent"/>
    <LinearLayout android:layout_width="match_parent" android:layout_height="wrap_content"
        android:layout_gravity="bottom" android:orientation="horizontal"
        android:padding="8dp" android:background="#CC000000">
        <Button android:id="@+id/btnRewarded"
            android:layout_width="0dp" android:layout_height="wrap_content"
            android:layout_weight="1" android:text="🎁 Watch Ad"
            android:textColor="#FFFFFF" android:backgroundTint="#4CAF50" android:layout_marginEnd="4dp"/>
        <Button android:id="@+id/btnInterstitial"
            android:layout_width="0dp" android:layout_height="wrap_content"
            android:layout_weight="1" android:text="📺 Show Ad"
            android:textColor="#FFFFFF" android:backgroundTint="#2196F3" android:layout_marginStart="4dp"/>
    </LinearLayout>{LOADING_OVERLAY_XML}
</FrameLayout>
"""

def make_layout_startapp():
    return f"""<?xml version="1.0" encoding="utf-8"?>
<FrameLayout xmlns:android="http://schemas.android.com/apk/res/android"
    android:layout_width="match_parent" android:layout_height="match_parent">
    <LinearLayout
        android:layout_width="match_parent" android:layout_height="match_parent"
        android:orientation="vertical">
        <WebView android:id="@+id/webView"
            android:layout_width="match_parent" android:layout_height="0dp" android:layout_weight="1"/>
        <FrameLayout android:id="@+id/bannerLayout"
            android:layout_width="match_parent" android:layout_height="wrap_content"/>
        <LinearLayout android:layout_width="match_parent" android:layout_height="wrap_content"
            android:orientation="horizontal" android:padding="8dp" android:background="#CC000000">
            <Button android:id="@+id/btnRewarded"
                android:layout_width="0dp" android:layout_height="wrap_content"
                android:layout_weight="1" android:text="🎁 Watch Ad"
                android:textColor="#FFFFFF" android:backgroundTint="#4CAF50" android:layout_marginEnd="4dp"/>
            <Button android:id="@+id/btnInterstitial"
                android:layout_width="0dp" android:layout_height="wrap_content"
                android:layout_weight="1" android:text="📺 Show Ad"
                android:textColor="#FFFFFF" android:backgroundTint="#FF6B35" android:layout_marginStart="4dp"/>
        </LinearLayout>
    </LinearLayout>{LOADING_OVERLAY_XML}
</FrameLayout>
"""

def make_strings(app_name):
    return f'<?xml version="1.0" encoding="utf-8"?>\n<resources>\n    <string name="app_name">{app_name}</string>\n</resources>\n'

def make_root_gradle():
    return """buildscript {
    repositories { google(); mavenCentral() }
    dependencies { classpath 'com.android.tools.build:gradle:8.5.2' }
}
allprojects { repositories { google(); mavenCentral() } }
task clean(type: Delete) { delete rootProject.buildDir }
"""

def make_app_gradle_unity(pkg):
    return f"""plugins {{ id 'com.android.application' }}
android {{
    namespace '{pkg}'
    compileSdk 35
    defaultConfig {{ applicationId "{pkg}"; minSdk 21; targetSdk 35; versionCode 1; versionName "1.0" }}
    buildTypes {{
        debug {{ minifyEnabled false; shrinkResources false }}
        release {{ minifyEnabled true; shrinkResources true;  proguardFiles getDefaultProguardFile('proguard-android-optimize.txt'), 'proguard-rules.pro' }}
    }}
    compileOptions {{ sourceCompatibility JavaVersion.VERSION_1_8; targetCompatibility JavaVersion.VERSION_1_8 }}
    aaptOptions {{
        noCompress 'html', 'js', 'css', 'png', 'jpg', 'jpeg', 'gif', 'svg', 'webp'
    }}
}}
dependencies {{
    implementation 'androidx.appcompat:appcompat:1.6.1'
    implementation 'com.unity3d.ads:unity-ads:4.9.2'
}}
"""

def make_app_gradle_startapp(pkg):
    return f"""plugins {{ id 'com.android.application' }}
android {{
    namespace '{pkg}'
    compileSdk 35
    defaultConfig {{ applicationId "{pkg}"; minSdk 21; targetSdk 35; versionCode 1; versionName "1.0" }}
    buildTypes {{
        debug {{ minifyEnabled false; shrinkResources false }}
        release {{ minifyEnabled true; shrinkResources true;  proguardFiles getDefaultProguardFile('proguard-android-optimize.txt'), 'proguard-rules.pro' }}
    }}
    compileOptions {{ sourceCompatibility JavaVersion.VERSION_1_8; targetCompatibility JavaVersion.VERSION_1_8 }}
    aaptOptions {{
        noCompress 'html', 'js', 'css', 'png', 'jpg', 'jpeg', 'gif', 'svg', 'webp'
    }}
}}
dependencies {{
    implementation 'androidx.appcompat:appcompat:1.6.1'
    implementation 'com.startapp:inapp-sdk:5.2.0'
}}
"""

def make_gradle_properties():
    return ("android.useAndroidX=true\nandroid.enableJetifier=true\n"
            "org.gradle.jvmargs=-Xmx2048m\n"
            "org.gradle.parallel=true\norg.gradle.caching=true\norg.gradle.configureondemand=true\n")

def make_settings(app_name):
    return f'rootProject.name = "{sanitize(app_name)}"\ninclude \':app\'\n'

def make_proguard(network="unity"):
    base = "-keep class com.unity3d.** { *; }\n-keep interface com.unity3d.** { *; }\n" if network == "unity" else "-keep class com.startapp.** { *; }\n-dontwarn com.startapp.**\n"
    base += "-keepclassmembers class * {\n    @android.webkit.JavascriptInterface <methods>;\n}\n"
    return base

def make_workflow(app_name):
    safe = sanitize(app_name)
    return f"""name: Build APK
on:
  push:
    branches: [ main ]
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Set up JDK 17
        uses: actions/setup-java@v4
        with:
          java-version: '17'
          distribution: 'temurin'
      - name: Setup Gradle
        uses: gradle/actions/setup-gradle@v4
        with:
          gradle-version: '8.7'
      - name: Build Debug APK
        run: gradle assembleDebug --no-daemon --parallel --build-cache
      - name: Upload APK
        uses: actions/upload-artifact@v4
        with:
          name: {safe}-debug
          path: app/build/outputs/apk/debug/app-debug.apk
          retention-days: 7
"""

# ── GitHub: single atomic commit ─────────────────────────────────────────────

def commit_all_files(files, message, binary_files=None):
    ref_resp, ref_status = gh("GET", f"/repos/{GITHUB_OWNER}/{GITHUB_REPO}/git/ref/heads/main")
    parents = [ref_resp["object"]["sha"]] if ref_status == 200 else []

    tree_items = [{"path": p, "mode": "100644", "type": "blob", "content": c}
                  for p, c in files.items()]

    for p, raw in (binary_files or {}).items():
        sha = create_blob(raw)
        if sha:
            tree_items.append({"path": p, "mode": "100644", "type": "blob", "sha": sha})

    tree_body = {"tree": tree_items}
    tree_resp, ts = gh("POST", f"/repos/{GITHUB_OWNER}/{GITHUB_REPO}/git/trees", tree_body)
    if ts not in (200, 201): return False, f"tree create failed: {tree_resp}"

    commit_body = {"message": message, "tree": tree_resp["sha"]}
    if parents: commit_body["parents"] = parents
    commit_resp, cs = gh("POST", f"/repos/{GITHUB_OWNER}/{GITHUB_REPO}/git/commits", commit_body)
    if cs not in (200, 201): return False, f"commit create failed: {commit_resp}"
    new_sha = commit_resp["sha"]

    if parents:
        _, rs = gh("PATCH", f"/repos/{GITHUB_OWNER}/{GITHUB_REPO}/git/refs/heads/main", {"sha": new_sha, "force": False})
    else:
        _, rs = gh("POST", f"/repos/{GITHUB_OWNER}/{GITHUB_REPO}/git/refs", {"ref": "refs/heads/main", "sha": new_sha})
    if rs not in (200, 201): return False, f"ref update failed ({rs})"
    return True, new_sha

def ensure_repo_exists():
    _, status = gh("GET", f"/repos/{GITHUB_OWNER}/{GITHUB_REPO}")
    if status == 404:
        gh("POST", "/user/repos", {"name": GITHUB_REPO, "private": False,
                                    "auto_init": False, "description": "APK Builder"})

def push_project(app_name, pkg, network, ad_id, content_type, content_value,
                 ad_types, auto_show, test_mode=False, icon_url="", permissions=None,
                 rewarded_placement="", entry_file="index.html", zip_files=None):
    ensure_repo_exists()
    pkg_path = pkg.replace(".", "/")

    has_rewarded     = "rewarded"     in ad_types
    has_interstitial = "interstitial" in ad_types
    has_banner       = "banner"       in ad_types

    if network == "unity":
        main_java  = make_main_activity_unity(pkg, ad_id, content_type, content_value,
                                              has_rewarded, has_interstitial, auto_show, test_mode,
                                              entry_file, permissions)
        app_gradle = make_app_gradle_unity(pkg)
        layout     = make_layout_unity()
        layout_key = "app/src/main/res/layout/activity_main.xml"
    else:  # startapp
        main_java  = make_main_activity_startapp(pkg, ad_id, content_type, content_value,
                                                 has_banner, has_interstitial, has_rewarded, auto_show,
                                                 test_mode, rewarded_placement, entry_file, permissions)
        app_gradle = make_app_gradle_startapp(pkg)
        layout     = make_layout_startapp()
        layout_key = "app/src/main/res/layout/activity_main_startapp.xml"

    # Icon
    icon_files = None
    icon_bytes = fetch_icon_bytes(icon_url, content_value if content_type == "url" else None)
    if icon_bytes:
        icon_files = build_icon_files(icon_bytes)

    # Build file dict
    files = {
        "build.gradle":                         make_root_gradle(),
        "settings.gradle":                      make_settings(app_name),
        "gradle.properties":                    make_gradle_properties(),
        "app/build.gradle":                     app_gradle,
        "app/proguard-rules.pro":               make_proguard(network),
        "app/src/main/AndroidManifest.xml":     make_manifest(pkg, app_name, network, has_icon=bool(icon_files), permissions=permissions),
        layout_key:                              layout,
        "app/src/main/res/values/strings.xml":  make_strings(app_name),
        f"app/src/main/java/{pkg_path}/MainActivity.java": main_java,
        ".github/workflows/build.yml":          make_workflow(app_name),
    }

    # Add zip assets if provided
    binary_files = {}
    if zip_files:
        for path, content in zip_files.items():
            asset_path = f"app/src/main/assets/{path}"
            binary_files[asset_path] = content

    # Merge icon files
    if icon_files:
        binary_files.update(icon_files)

    return commit_all_files(files, f"Build: {app_name}", binary_files=binary_files)

# ─── ROUTES ──────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    host = request.host_url.rstrip("/")
    return render_template("index.html", github_owner=GITHUB_OWNER,
                           github_repo=GITHUB_REPO, host=host)

@app.route("/build", methods=["POST"])
def build():
    if not GITHUB_TOKEN or not GITHUB_OWNER:
        return jsonify({"success": False, "error": "Server config missing"}), 500

    if request.is_json:
        d = request.json or {}
    else:
        d = request.form.to_dict()

    app_name      = d.get("app_name", "").strip()
    pkg           = d.get("package_name", "").strip()
    network       = d.get("network", "unity")
    ad_id         = d.get("ad_id", "").strip()
    content_type  = d.get("content_type", "url")
    content_value = d.get("content_value", "").strip()
    ad_types      = d.get("ad_types", ["rewarded", "interstitial"])
    if isinstance(ad_types, str):
        ad_types = [x.strip() for x in ad_types.split(",") if x.strip()]
    auto_show     = bool(d.get("auto_show", False))
    test_mode     = bool(d.get("test_mode", False))
    icon_url      = d.get("icon_url", "").strip()
    permissions   = d.get("permissions", [])
    if isinstance(permissions, str):
        permissions = [x.strip() for x in permissions.split(",") if x.strip()]
    rewarded_placement = d.get("rewarded_placement", "").strip()
    entry_file    = d.get("entry_file", "index.html").strip()
    zip_base64    = d.get("zip_base64", "")

    zip_files = None
    if content_type == "zip" and zip_base64:
        try:
            zip_data = base64.b64decode(zip_base64)
            zip_buffer = io.BytesIO(zip_data)
            with zipfile.ZipFile(zip_buffer, 'r') as zf:
                file_list = {}
                for info in zf.infolist():
                    if info.filename.endswith('/'): continue
                    name = info.filename.replace('\\', '/')
                    if name.startswith('/'): name = name[1:]
                    content = zf.read(info)
                    file_list[name] = content
                zip_files = file_list
                if not entry_file or entry_file not in file_list:
                    for f in file_list:
                        if f.lower().endswith('index.html') or f.lower().endswith('index.htm'):
                            entry_file = f
                            break
                    else:
                        entry_file = list(file_list.keys())[0]
        except Exception as e:
            return jsonify({"success": False, "error": f"Zip processing failed: {e}"}), 400

    # Validation
    errors = []
    if not app_name:         errors.append("App name required")
    if not valid_pkg(pkg):   errors.append("Valid package name required (e.g. com.company.app)")
    if not ad_id:            errors.append("Ad Network ID required")
    if content_type == "url" and (not content_value or not content_value.startswith(("http://", "https://"))):
        errors.append("Valid URL required")
    if content_type == "html" and not content_value:
        errors.append("HTML content required")
    if content_type == "zip" and not zip_files:
        errors.append("ZIP file content required")
    if not ad_types:         errors.append("Ek ad type select karo")
    if errors:
        return jsonify({"success": False, "error": " | ".join(errors)}), 400

    ok, result = push_project(app_name, pkg, network, ad_id,
                              content_type, content_value if content_type != "zip" else "",
                              ad_types, auto_show, test_mode, icon_url,
                              permissions, rewarded_placement, entry_file, zip_files)
    if not ok:
        return jsonify({"success": False, "error": f"GitHub push failed: {result}"}), 500

    has_rewarded     = "rewarded"     in ad_types
    has_interstitial = "interstitial" in ad_types
    accent = "#4CAF50" if network == "unity" else "#4CAF50"
    r_btn = ('<a href="ads://show_rewarded" style="display:inline-block;padding:12px 20px;'
              f'background:{accent};color:#fff;border-radius:8px;text-decoration:none;font-weight:bold;">'
              '🎁 Watch Ad for Reward</a>') if has_rewarded else ""
    i_btn = ('<a href="ads://show_interstitial" style="display:inline-block;padding:12px 20px;'
              'background:#2196F3;color:#fff;border-radius:8px;text-decoration:none;font-weight:bold;">'
              '📺 Show Ad</a>') if has_interstitial else ""
    js_r  = "window.NativeAds.showRewarded()"
    js_i  = "window.NativeAds.showInterstitial()"

    return jsonify({
        "success":      True,
        "commit_sha":   result,
        "actions_url":  f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}/actions",
        "repo_url":     f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}",
        "message":      f"✅ '{app_name}' push ho gaya!",
        "snippets": {
            "rewarded_btn":     r_btn,
            "interstitial_btn": i_btn,
            "js_rewarded":      js_r,
            "js_interstitial":  js_i,
        }
    })

# ─── Custom Ad API ──────────────────────────────────────────────────────────

@app.route("/api/generate", methods=["POST"])
def api_generate():
    d        = request.json or {}
    game_id  = d.get("game_id", "").strip()
    network  = d.get("network", "unity")
    ad_types = d.get("ad_types", ["rewarded", "interstitial"])
    if not game_id:
        return jsonify({"success": False, "error": "Game/App ID required"}), 400
    if not ad_types:
        return jsonify({"success": False, "error": "Kam se kam ek ad type select karo"}), 400

    has_r = "rewarded"     in ad_types
    has_i = "interstitial" in ad_types
    has_b = "banner"       in ad_types

    api_key = uuid.uuid4().hex[:24]
    AD_SESSIONS[api_key] = {"game_id": game_id, "network": network, "ad_types": ad_types}
    host = request.host_url.rstrip("/")
    endpoint = f"{host}/api/show/{api_key}"

    js_funcs, js_buttons, url_links, test_buttons, test_urls = [], [], [], [], []

    if has_r:
        js_funcs.append("""function showRewarded() {
  fetch('""" + endpoint + """?type=rewarded', {method:'POST'})
    .then(r=>r.json())
    .then(d=>{ if(d.success && window.NativeAds) window.NativeAds.showRewarded(); });
}""")
        js_buttons.append('<button onclick="showRewarded()">🎁 Watch Ad for Reward</button>')
        url_links.append('<a href="ads://show_rewarded">🎁 Watch Ad for Reward</a>')
        test_buttons.append('<button class="btn" style="background:#4CAF50;color:#fff" onclick="showRewarded()">🎁 Watch Rewarded Ad</button>')
        test_urls.append('<a href="ads://show_rewarded" class="btn" style="background:#7c3aed;color:#fff">🎁 Rewarded (URL)</a>')

    if has_i:
        js_funcs.append("""function showInterstitial() {
  fetch('""" + endpoint + """?type=interstitial', {method:'POST'})
    .then(r=>r.json())
    .then(d=>{ if(d.success && window.NativeAds) window.NativeAds.showInterstitial(); });
}""")
        js_buttons.append('<button onclick="showInterstitial()">📺 Show Ad</button>')
        url_links.append('<a href="ads://show_interstitial">📺 Show Ad</a>')
        test_buttons.append('<button class="btn" style="background:#2196F3;color:#fff" onclick="showInterstitial()">📺 Show Interstitial</button>')
        test_urls.append('<a href="ads://show_interstitial" class="btn" style="background:#0891b2;color:#fff">📺 Interstitial (URL)</a>')

    banner_note = ""
    if has_b:
        banner_note = ("\n<!-- 📰 Banner ad koi button/JS nahi maangta — app ke andar\n"
                        "     WebView ke upar/niche apne aap dikhta rehta hai. -->")

    snippet = "<!-- Copy this into your HTML -->\n<script>\n" + "\n\n".join(js_funcs) + \
        "\n</script>\n\n<!-- Paste onclick on ANY button in your HTML -->\n" + \
        "\n".join(js_buttons) + banner_note

    url_snippet = "<!-- OR use URL bridge — no JS needed! -->\n" + "\n".join(url_links) + banner_note

    banner_test_note = '<p style="color:#888;margin-top:20px">📰 Banner ad app ke andar khud-ba-khud dikhta hai.</p>' if has_b else ""

    test_html = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Ad Test</title>
<script>
{chr(10).join(js_funcs)}
</script>
<style>body{{font-family:sans-serif;text-align:center;padding:40px;background:#111;color:#fff}}
.btn{{display:inline-block;margin:10px;padding:14px 28px;border-radius:10px;font-size:16px;font-weight:bold;text-decoration:none;cursor:pointer;border:none;}}
</style></head><body>
<h2>Ad Test Page</h2>
{chr(10).join(test_buttons)}
{banner_test_note}
<hr style="border-color:#333;margin:30px 0">
<p style="color:#888">URL Bridge (no JS needed):</p>
{chr(10).join(test_urls)}
</body></html>"""

    return jsonify({
        "success": True, "api_key": api_key,
        "endpoint": endpoint, "network": network,
        "game_id": game_id, "ad_types": ad_types,
        "js_snippet": snippet,
        "url_snippet": url_snippet,
        "test_html": test_html,
    })

@app.route("/api/show/<api_key>", methods=["POST", "GET", "OPTIONS"])
def api_show(api_key):
    if request.method == "OPTIONS":
        r = jsonify({})
        r.headers["Access-Control-Allow-Origin"]  = "*"
        r.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
        r.headers["Access-Control-Allow-Headers"] = "Content-Type"
        return r
    s = AD_SESSIONS.get(api_key)
    if not s:
        return jsonify({"success": False, "error": "Invalid API key"}), 404
    ad_type = request.args.get("type", "rewarded")
    if ad_type not in s["ad_types"]:
        r = jsonify({"success": False,
                     "error": f"'{ad_type}' is tick nahi kiya tha jab API banaya tha"})
        r.headers["Access-Control-Allow-Origin"] = "*"
        return r, 400
    r = jsonify({"success": True, "ad_type": ad_type, "network": s["network"],
                 "game_id": s["game_id"], "message": f"{ad_type} ad trigger sent"})
    r.headers["Access-Control-Allow-Origin"] = "*"
    return r

# ─── Build status polling ──────────────────────────────────────────────────

@app.route("/check-package")
def check_package():
    pkg = request.args.get("pkg", "").strip()
    return jsonify({"valid": valid_pkg(pkg)})

@app.route("/find-run/<sha>")
def find_run(sha):
    resp, status = gh("GET", f"/repos/{GITHUB_OWNER}/{GITHUB_REPO}/actions/runs?head_sha={sha}")
    if status == 200 and resp.get("workflow_runs"):
        run = resp["workflow_runs"][0]
        return jsonify({"found": True, "run_id": run["id"],
                        "status": run["status"], "conclusion": run.get("conclusion"),
                        "html_url": run["html_url"]})
    return jsonify({"found": False})

@app.route("/run-status/<int:run_id>")
def run_status(run_id):
    resp, status = gh("GET", f"/repos/{GITHUB_OWNER}/{GITHUB_REPO}/actions/runs/{run_id}")
    if status != 200: return jsonify({"error": "not found"}), 404
    return jsonify({"status": resp["status"], "conclusion": resp.get("conclusion"),
                    "html_url": resp["html_url"]})

@app.route("/download/<int:run_id>")
def download(run_id):
    resp, status = gh("GET", f"/repos/{GITHUB_OWNER}/{GITHUB_REPO}/actions/runs/{run_id}/artifacts")
    if status != 200 or not resp.get("artifacts"):
        return "APK abhi nahi mila. Wait karo.", 404
    artifact = resp["artifacts"][0]
    req = urllib.request.Request(artifact["archive_download_url"])
    req.add_header("Authorization", f"token {GITHUB_TOKEN}")
    req.add_header("Accept", "application/vnd.github+json")
    try:
        with urllib.request.urlopen(req) as r:
            zip_bytes = r.read()
    except urllib.error.HTTPError as e:
        return f"Download failed: {e.code}", 500
    zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    apk_entry = next((n for n in zf.namelist() if n.endswith(".apk")), None)
    if not apk_entry: return "APK file nahi mili", 500
    apk_bytes = zf.read(apk_entry)
    filename  = f"{sanitize(artifact['name'])}.apk"
    return Response(apk_bytes, mimetype="application/vnd.android.package-archive",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)