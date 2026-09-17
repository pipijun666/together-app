package app.together.reader;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.Intent;
import android.graphics.Color;
import android.net.Uri;
import android.os.Bundle;
import android.view.View;
import android.view.ViewGroup;
import android.webkit.CookieManager;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.TextView;
import java.io.ByteArrayInputStream;

/** Native Android shell. Minimal JS bridge for modal navigation, no external SDKs.
 *  Release build is hardwired to FIXED_ORIGIN (no address input, no switch entry).
 *  Only --dev-local builds keep the local-address setup screen for USB testing. */
public class MainActivity extends Activity {
    private static final String FIXED_ORIGIN = BuildConfig.FIXED_ORIGIN;
    private WebView web;
    private LinearLayout topBar;
    private FrameLayout fullscreenView;
    private View customView;
    private WebChromeClient.CustomViewCallback customViewCallback;
    private String origin = "";
    private String pending = "";
    private boolean pageFailed = false;
    private ValueCallback<Uri[]> fileCallback;
    private static final int FILE_PICK = 41;
    private static final int MAX_SHARE_TEXT = 10000;
    private volatile boolean isModalOpen = false;
    private long lastBackTime = 0;
    private final int paper = Color.rgb(251,248,241);
    private final int ink = Color.rgb(48,47,41);
    private final int orange = Color.rgb(237,112,75);

    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        getWindow().setStatusBarColor(paper);
        getWindow().setNavigationBarColor(paper);
        getWindow().getDecorView().setSystemUiVisibility(View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR | View.SYSTEM_UI_FLAG_LIGHT_NAVIGATION_BAR);
        readShare(getIntent());
        String saved = getPreferences(MODE_PRIVATE).getString("origin", "");
        if (BuildConfig.ALLOW_LOCAL_DEBUG) {
            // dev-only build: keep the local-address entry for USB testing.
            if (valid(saved)) { origin=saved; showWeb(); } else showSetup();
        } else {
            // release: fixed public origin; any previously saved address is
            // migrated to it. Cookies stay domain-scoped by the WebView, so we
            // neither copy the old origin's session nor wipe other origins.
            origin = FIXED_ORIGIN;
            showWeb();
        }
    }
    private int dp(int value) { return (int)(value*getResources().getDisplayMetrics().density+.5f); }
    private LinearLayout root() {
        LinearLayout root=new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setBackgroundColor(paper);
        root.setOnApplyWindowInsetsListener((v,insets)->{
            v.setPadding(insets.getSystemWindowInsetLeft(),insets.getSystemWindowInsetTop(),insets.getSystemWindowInsetRight(),insets.getSystemWindowInsetBottom());
            return insets.consumeSystemWindowInsets();
        });
        setContentView(root);
        return root;
    }
    private TextView label(String value, int size) {
        TextView t=new TextView(this); t.setText(value);t.setTextSize(size);t.setTextColor(ink);return t;
    }
    private boolean valid(String value) {
        try {
            Uri u=Uri.parse(value);
            String host=u.getHost();
            boolean local="127.0.0.1".equals(host)||"localhost".equals(host);
            return host!=null && u.getUserInfo()==null && u.getQuery()==null && u.getFragment()==null &&
                (u.getPath()==null||u.getPath().isEmpty()||u.getPath().equals("/")) &&
                ("https".equals(u.getScheme())||("http".equals(u.getScheme())&&local));
        } catch(Exception e) { return false; }
    }
    private void showSetup() {
        if(web!=null) { web.stopLoading(); web.destroy(); web=null; }
        LinearLayout root=root();
        LinearLayout panel=new LinearLayout(this);panel.setOrientation(LinearLayout.VERTICAL);panel.setPadding(dp(28),dp(64),dp(28),dp(28));root.addView(panel);
        panel.addView(label("一起看  /",32));
        TextView intro=label("有趣的事，想和你一起看。\n\n填写你们的空间地址，\n连上后就能登录、分享和回应。",16);
        intro.setLineSpacing(dp(7),1);intro.setPadding(0,dp(32),0,dp(28));panel.addView(intro);
        EditText address=new EditText(this);address.setSingleLine(true);address.setText(origin);address.setHint("https://你的国内服务域名");address.setTextSize(15);address.setInputType(android.text.InputType.TYPE_CLASS_TEXT|android.text.InputType.TYPE_TEXT_VARIATION_URI);panel.addView(address,new LinearLayout.LayoutParams(-1,dp(58)));
        Button connect=new Button(this);connect.setText("进入我们的小空间");connect.setTextColor(Color.WHITE);connect.setBackgroundTintList(android.content.res.ColorStateList.valueOf(orange));
        LinearLayout.LayoutParams bp=new LinearLayout.LayoutParams(-1,dp(56));bp.topMargin=dp(20);panel.addView(connect,bp);
        TextView hint=label("灰度测试版 0.4 · 仅连接你填写的 HTTPS 空间地址\n服务器尚未部署时，请先按随包说明启动服务。",12);hint.setTextColor(Color.rgb(135,136,123));hint.setPadding(0,dp(25),0,0);hint.setLineSpacing(dp(5),1);panel.addView(hint);
        connect.setOnClickListener(v->{String value=address.getText().toString().trim().replaceAll("/+$", "");if(!valid(value)){address.setError("请填写 HTTPS 空间地址，不包含路径；USB 本机测试可用 http://127.0.0.1:8765");return;}origin=value;getPreferences(MODE_PRIVATE).edit().putString("origin",origin).apply();showWeb();});
    }
    private boolean sameOrigin(Uri u) {
        Uri base=Uri.parse(origin);
        int up=u.getPort()<0?("https".equals(u.getScheme())?443:80):u.getPort();
        int bp=base.getPort()<0?("https".equals(base.getScheme())?443:80):base.getPort();
        return base.getScheme().equals(u.getScheme()) && base.getHost().equalsIgnoreCase(u.getHost()==null?"":u.getHost()) && bp==up;
    }
    private void showWeb() {
        LinearLayout root=root();
        topBar=new LinearLayout(this);topBar.setGravity(android.view.Gravity.CENTER_VERTICAL);topBar.setPadding(dp(17),0,dp(17),0);root.addView(topBar,new LinearLayout.LayoutParams(-1,dp(42)));
        TextView title=label("一起看 · 私密空间",12);topBar.addView(title,new LinearLayout.LayoutParams(-1,-2,1));
        LinearLayout errorPanel=new LinearLayout(this);errorPanel.setOrientation(LinearLayout.VERTICAL);errorPanel.setPadding(dp(26),dp(65),dp(26),dp(26));errorPanel.setVisibility(View.GONE);root.addView(errorPanel,new LinearLayout.LayoutParams(-1,0,1));
        errorPanel.addView(label("暂时连不上我们的小空间",23));
        TextView errorMessage=label("",15);errorMessage.setPadding(0,dp(24),0,dp(20));errorMessage.setLineSpacing(dp(6),1);errorPanel.addView(errorMessage);
        Button retry=new Button(this);retry.setText("重试");retry.setTextColor(Color.WHITE);retry.setBackgroundTintList(android.content.res.ColorStateList.valueOf(orange));
        LinearLayout.LayoutParams rp=new LinearLayout.LayoutParams(-1,dp(54));rp.topMargin=dp(8);errorPanel.addView(retry,rp);
        errorPanel.addView(label("重试会重新连接我们的小空间。\n\n已保存在服务器的内容不会因此丢失。",13));
        web=new WebView(this);web.setBackgroundColor(paper);root.addView(web,new LinearLayout.LayoutParams(-1,0,1));
        fullscreenView=new FrameLayout(this);fullscreenView.setBackgroundColor(Color.BLACK);fullscreenView.setVisibility(View.GONE);root.addView(fullscreenView,new FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT,ViewGroup.LayoutParams.MATCH_PARENT));
        WebSettings s=web.getSettings();s.setJavaScriptEnabled(true);s.setDomStorageEnabled(true);s.setAllowFileAccess(false);s.setAllowContentAccess(true);s.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);s.setSafeBrowsingEnabled(true);s.setJavaScriptCanOpenWindowsAutomatically(false);s.setSupportMultipleWindows(false);s.setCacheMode(WebSettings.LOAD_DEFAULT);
        CookieManager.getInstance().setAcceptCookie(true);CookieManager.getInstance().setAcceptThirdPartyCookies(web,false);
        web.addJavascriptInterface(new Object() {
            @android.webkit.JavascriptInterface
            public void setModalOpen(boolean open) {
                isModalOpen = open;
            }
        }, "AndroidBridge");
        web.setWebViewClient(new WebViewClient(){
            private void fail(String message){pageFailed=true;title.setText("连接未完成");errorMessage.setText(message);errorPanel.setVisibility(View.VISIBLE);web.setVisibility(View.GONE);}
            @Override public void onPageStarted(WebView view,String url,android.graphics.Bitmap favicon){
                android.util.Log.i("TogetherApp", "onPageStarted: " + url);
                pageFailed=false;isModalOpen=false;errorPanel.setVisibility(View.GONE);view.setVisibility(View.VISIBLE);title.setText("正在连接空间…");
            }
            @Override public boolean shouldOverrideUrlLoading(WebView view,WebResourceRequest request){
                Uri uri=request.getUrl();if(sameOrigin(uri))return false;
                if(request.isForMainFrame()&&request.hasGesture()&&("https".equals(uri.getScheme())||"http".equals(uri.getScheme()))) {
                    new AlertDialog.Builder(MainActivity.this).setTitle("打开原始出处？").setMessage("将使用浏览器打开原网站，是否能访问取决于当前网络。").setPositiveButton("打开",(d,w)->{try{startActivity(new Intent(Intent.ACTION_VIEW,uri));}catch(Exception ignored){}}).setNegativeButton("取消",null).show();
                }
                return true;
            }
            @Override public WebResourceResponse shouldInterceptRequest(WebView view,WebResourceRequest request){
                if(!sameOrigin(request.getUrl())){
                    android.util.Log.w("TogetherApp", "Blocked external resource: " + request.getUrl());
                    return new WebResourceResponse("text/plain","UTF-8",403,"External resource blocked",null,new ByteArrayInputStream(new byte[0]));
                }
                return super.shouldInterceptRequest(view,request);
            }
            @Override public void onReceivedError(WebView view,WebResourceRequest request,android.webkit.WebResourceError error){
                android.util.Log.e("TogetherApp", "onReceivedError: " + request.getUrl() + " code=" + error.getErrorCode() + " desc=" + error.getDescription());
                if(request.isForMainFrame())fail(error.getErrorCode()==ERROR_HOST_LOOKUP?"找不到这个域名，请检查空间地址的拼写和网络连接。":"网络连接失败或等待超时。请确认国内服务器已启动，且手机可以访问该地址。");
            }
            @Override public void onReceivedHttpError(WebView view,WebResourceRequest request,WebResourceResponse response){
                android.util.Log.e("TogetherApp", "onReceivedHttpError: " + request.getUrl() + " status=" + response.getStatusCode());
                if(request.isForMainFrame())fail("空间服务返回 HTTP "+response.getStatusCode()+"。请检查服务地址和服务器配置后重试。");
            }
            @Override public void onReceivedSslError(WebView view,android.webkit.SslErrorHandler handler,android.net.http.SslError error){
                android.util.Log.e("TogetherSSL", "SSL error: " + error.getPrimaryError() + " url: " + error.getUrl());
                handler.cancel();
                fail("空间的 HTTPS 证书验证未通过。请先修复域名或证书配置，再重新连接。");
            }
            @Override public void onPageFinished(WebView view,String url){
                android.util.Log.i("TogetherApp", "onPageFinished: " + url + " pageFailed=" + pageFailed);
                if(!pageFailed)title.setText("一起看 · 私密空间");
                CookieManager.getInstance().flush();
            }
        });
        web.setWebChromeClient(new WebChromeClient(){
            @Override public boolean onShowFileChooser(WebView view,ValueCallback<Uri[]> callback,FileChooserParams params){
                if(fileCallback!=null)fileCallback.onReceiveValue(null);fileCallback=callback;
                Intent pick=new Intent(Intent.ACTION_OPEN_DOCUMENT);pick.addCategory(Intent.CATEGORY_OPENABLE);pick.setType("image/*");
                try{startActivityForResult(pick,FILE_PICK);}catch(Exception e){fileCallback.onReceiveValue(null);fileCallback=null;}return true;
            }
            @Override public boolean onConsoleMessage(android.webkit.ConsoleMessage message){
                android.util.Log.d("TogetherWeb", message.message()+" ["+message.sourceId()+":"+message.lineNumber()+"]");
                return true;
            }
            @Override public boolean onJsConfirm(WebView view,String url,String message,android.webkit.JsResult result){
                new AlertDialog.Builder(MainActivity.this).setMessage(message).setPositiveButton("确定",(d,w)->result.confirm()).setNegativeButton("取消",(d,w)->result.cancel()).setOnCancelListener(d->result.cancel()).show();return true;
            }
            @Override public void onShowCustomView(View view,android.webkit.WebChromeClient.CustomViewCallback callback){
                if(customView!=null){callback.onCustomViewHidden();return;}
                customView=view;customViewCallback=callback;
                fullscreenView.addView(view,new FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT,ViewGroup.LayoutParams.MATCH_PARENT));
                fullscreenView.setVisibility(View.VISIBLE);
                web.setVisibility(View.GONE);
                topBar.setVisibility(View.GONE);
                getWindow().getDecorView().setSystemUiVisibility(View.SYSTEM_UI_FLAG_FULLSCREEN|View.SYSTEM_UI_FLAG_HIDE_NAVIGATION|View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY);
                // Match the fullscreen orientation to the actual video aspect ratio:
                // portrait videos stay portrait, landscape videos rotate to landscape.
                try {
                    web.evaluateJavascript("(function(){var v=document.querySelector('video');return v?{w:v.videoWidth||0,h:v.videoHeight||0}:null})()", value -> {
                        int target=android.content.pm.ActivityInfo.SCREEN_ORIENTATION_UNSPECIFIED;
                        try {
                            if(value!=null&&!value.equals("null")) {
                                org.json.JSONObject o=new org.json.JSONObject(value);
                                int w=o.optInt("w",0),h=o.optInt("h",0);
                                if(w>h) target=android.content.pm.ActivityInfo.SCREEN_ORIENTATION_LANDSCAPE;
                                else if(h>w) target=android.content.pm.ActivityInfo.SCREEN_ORIENTATION_PORTRAIT;
                            }
                        } catch(Exception ignored) {}
                        setRequestedOrientation(target);
                    });
                } catch(Exception ignored) {
                    setRequestedOrientation(android.content.pm.ActivityInfo.SCREEN_ORIENTATION_LANDSCAPE);
                }
            }
            @Override public void onHideCustomView(){exitCustomView();}
        });
        retry.setOnClickListener(v->web.reload());
        web.loadUrl(origin+"/"+(pending.isEmpty()?"":"#share="+Uri.encode(pending)));pending="";
    }
    private void readShare(Intent intent) {
        if(Intent.ACTION_SEND.equals(intent.getAction())) {
            String value=intent.getStringExtra(Intent.EXTRA_TEXT);
            if(value!=null&&!value.trim().isEmpty()) {
                pending=value.length()>MAX_SHARE_TEXT?value.substring(0,MAX_SHARE_TEXT):value;
            }
        }
    }
    @Override protected void onNewIntent(Intent intent){super.onNewIntent(intent);setIntent(intent);readShare(intent);if(!origin.isEmpty()&&web!=null&&!pending.isEmpty()){web.loadUrl(origin+"/#share="+Uri.encode(pending));pending="";}}
    @Override protected void onActivityResult(int request,int result,Intent data){super.onActivityResult(request,result,data);if(request==FILE_PICK&&fileCallback!=null){fileCallback.onReceiveValue(result==RESULT_OK&&data!=null&&data.getData()!=null?new Uri[]{data.getData()}:null);fileCallback=null;}}
    private void exitCustomView(){
        if(customView==null)return;
        customViewCallback.onCustomViewHidden();
        fullscreenView.removeView(customView);
        fullscreenView.setVisibility(View.GONE);
        web.setVisibility(View.VISIBLE);
        topBar.setVisibility(View.VISIBLE);
        getWindow().getDecorView().setSystemUiVisibility(View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR|View.SYSTEM_UI_FLAG_LIGHT_NAVIGATION_BAR);
        setRequestedOrientation(android.content.pm.ActivityInfo.SCREEN_ORIENTATION_UNSPECIFIED);
        customView=null;customViewCallback=null;
    }
    @Override public void onBackPressed(){
        if(customView!=null){exitCustomView();return;}
        if(web!=null){
            if(isModalOpen){
                isModalOpen=false;
                web.evaluateJavascript("if(window.closeModal)window.closeModal(true);",null);
                return;
            }
            if(web.canGoBack()){web.goBack();return;}
        }
        if(System.currentTimeMillis()-lastBackTime>2000){
            lastBackTime=System.currentTimeMillis();
            android.widget.Toast.makeText(this,"再按一次退出 一起看",android.widget.Toast.LENGTH_SHORT).show();
            return;
        }
        super.onBackPressed();
    }
    @Override protected void onDestroy(){if(fileCallback!=null){fileCallback.onReceiveValue(null);fileCallback=null;}if(web!=null)web.destroy();super.onDestroy();}
}
