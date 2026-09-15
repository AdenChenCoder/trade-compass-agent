package com.tradecompass.mobile.dev;

import android.content.Context;
import android.security.keystore.KeyGenParameterSpec;
import android.security.keystore.KeyProperties;
import android.util.Base64;
import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;
import org.json.JSONObject;
import org.json.JSONTokener;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.security.KeyStore;
import java.security.MessageDigest;
import java.security.SecureRandom;
import java.security.cert.X509Certificate;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import javax.crypto.Cipher;
import javax.crypto.KeyGenerator;
import javax.crypto.SecretKey;
import javax.crypto.spec.GCMParameterSpec;
import javax.net.ssl.HttpsURLConnection;
import javax.net.ssl.SSLContext;
import javax.net.ssl.TrustManager;
import javax.net.ssl.X509TrustManager;

@CapacitorPlugin(name = "Compass")
public class CompassPlugin extends Plugin {
    private final ExecutorService work = Executors.newSingleThreadExecutor();
    private static final String KEY = "compass.device.v1";

    private SecretKey key() throws Exception {
        KeyStore store = KeyStore.getInstance("AndroidKeyStore");
        store.load(null);
        if (!store.containsAlias(KEY)) {
            KeyGenerator generator = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore");
            generator.init(new KeyGenParameterSpec.Builder(KEY, KeyProperties.PURPOSE_ENCRYPT | KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM).setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE).build());
            return generator.generateKey();
        }
        return (SecretKey) store.getKey(KEY, null);
    }

    private JSONObject state() throws Exception {
        String text = getContext().getSharedPreferences(KEY, Context.MODE_PRIVATE).getString("state", null);
        if (text == null) return null;
        JSONObject stored = new JSONObject(text);
        Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
        cipher.init(Cipher.DECRYPT_MODE, key(), new GCMParameterSpec(128, Base64.decode(stored.getString("iv"), Base64.NO_WRAP)));
        return new JSONObject(new String(cipher.doFinal(Base64.decode(stored.getString("data"), Base64.NO_WRAP)), StandardCharsets.UTF_8));
    }

    private void save(JSONObject state) throws Exception {
        Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
        cipher.init(Cipher.ENCRYPT_MODE, key());
        JSONObject stored = new JSONObject().put("iv", Base64.encodeToString(cipher.getIV(), Base64.NO_WRAP))
            .put("data", Base64.encodeToString(cipher.doFinal(state.toString().getBytes(StandardCharsets.UTF_8)), Base64.NO_WRAP));
        if (!getContext().getSharedPreferences(KEY, Context.MODE_PRIVATE).edit().putString("state", stored.toString()).commit())
            throw new Exception("无法保存设备凭据");
    }

    private void validate(JSONObject data) throws Exception {
        URI endpoint = new URI(data.getString("endpoint"));
        if (!"https".equals(endpoint.getScheme()) || endpoint.getHost() == null || endpoint.getUserInfo() != null
            || endpoint.getQuery() != null || endpoint.getFragment() != null
            || !(endpoint.getPath().isEmpty() || "/".equals(endpoint.getPath()))
            || !data.getString("certificate_sha256").matches("[a-f0-9]{64}")) throw new Exception("电脑连接信息无效");
    }

    private JSObject send(JSONObject state, String method, String path, String body, boolean authorize) throws Exception {
        validate(state);
        if (!path.startsWith("/mobile/v1/") || path.contains("..") || !(method.equals("GET") || method.equals("POST")))
            throw new Exception("请求无效");
        final String pin = state.getString("certificate_sha256");
        SSLContext context = SSLContext.getInstance("TLS");
        context.init(null, new TrustManager[]{new X509TrustManager() {
            public X509Certificate[] getAcceptedIssuers() { return new X509Certificate[0]; }
            public void checkClientTrusted(X509Certificate[] chain, String auth) throws java.security.cert.CertificateException {
                throw new java.security.cert.CertificateException("Client certificates not supported");
            }
            public void checkServerTrusted(X509Certificate[] chain, String auth) throws java.security.cert.CertificateException {
                try {
                    if (chain.length == 0) throw new Exception("Missing certificate");
                    chain[0].checkValidity();
                    byte[] hash = MessageDigest.getInstance("SHA-256").digest(chain[0].getEncoded());
                    StringBuilder actual = new StringBuilder();
                    for (byte b : hash) actual.append(String.format("%02x", b));
                    if (!MessageDigest.isEqual(actual.toString().getBytes(StandardCharsets.US_ASCII), pin.getBytes(StandardCharsets.US_ASCII)))
                        throw new Exception("Computer identity changed");
                } catch (Exception error) { throw new java.security.cert.CertificateException("电脑身份校验失败，请在电脑端重新核对", error); }
            }
        }}, new SecureRandom());
        String origin = state.getString("endpoint").replaceAll("/$", "");
        HttpsURLConnection conn = (HttpsURLConnection) new URI(origin + path).toURL().openConnection();
        conn.setSSLSocketFactory(context.getSocketFactory());
        // Only this connection uses exact leaf-certificate identity instead of a DNS CA policy.
        conn.setHostnameVerifier((host, session) -> true);
        conn.setInstanceFollowRedirects(false);
        conn.setConnectTimeout(10000); conn.setReadTimeout(20000);
        conn.setRequestMethod(method);
        conn.setRequestProperty("Content-Type", "application/json");
        if (authorize) conn.setRequestProperty("Authorization", "Bearer " + state.getString("device_secret"));
        try {
            if (body != null) {
                conn.setDoOutput(true);
                try (java.io.OutputStream output = conn.getOutputStream()) { output.write(body.getBytes(StandardCharsets.UTF_8)); }
            }
            int status = conn.getResponseCode();
            java.io.InputStream input = status < 400 ? conn.getInputStream() : conn.getErrorStream();
            if (input == null) throw new Exception("电脑未返回有效响应");
            try (input; java.io.ByteArrayOutputStream output = new java.io.ByteArrayOutputStream()) {
                byte[] chunk = new byte[8192]; int count;
                while ((count = input.read(chunk)) != -1) {
                    if (output.size() + count > 16 * 1024 * 1024) throw new Exception("历史内容过大，请减少每页消息数");
                    output.write(chunk, 0, count);
                }
                JSObject result = new JSObject(); result.put("status", status);
                result.put("data", new JSONTokener(output.toString("UTF-8")).nextValue());
                return result;
            }
        } finally { conn.disconnect(); }
    }

    @PluginMethod public void connection(PluginCall call) { work.execute(() -> {
        try { JSONObject state = state(); JSObject result = new JSObject(); result.put("connected", state != null);
            if (state != null) { result.put("endpoint", state.getString("endpoint")); result.put("computer_id", state.getString("computer_id")); }
            call.resolve(result);
        } catch (Exception e) { call.reject("无法读取设备凭据，请移除连接后重新配对"); }
    }); }

    @PluginMethod public void pair(PluginCall call) { work.execute(() -> {
        try {
            if (state() != null) throw new Exception("请先移除已有电脑连接");
            JSONObject invite = new JSONObject(call.getString("invitation", "")); validate(invite);
            if (invite.getInt("protocol_version") != 1 || invite.getDouble("expires_at") * 1000 <= System.currentTimeMillis()
                || !invite.getString("invitation").matches("[A-Za-z0-9_-]{43}")) throw new Exception("配对信息已过期或无效");
            byte[] random = new byte[32]; new SecureRandom().nextBytes(random);
            JSONObject state = new JSONObject().put("endpoint", invite.getString("endpoint"))
                .put("computer_id", invite.getString("computer_id")).put("certificate_sha256", invite.getString("certificate_sha256"))
                .put("device_secret", Base64.encodeToString(random, Base64.URL_SAFE | Base64.NO_WRAP | Base64.NO_PADDING));
            save(state);
            JSONObject body = new JSONObject().put("invitation", invite.getString("invitation"))
                .put("device_secret", state.getString("device_secret")).put("name", call.getString("name", "Android 手机"));
            call.resolve(send(state, "POST", "/mobile/v1/pairing/claim", body.toString(), false));
        } catch (Exception e) { call.reject(e.getMessage() == null ? "连接失败，请检查电脑地址和网络" : e.getMessage()); }
    }); }

    @PluginMethod public void request(PluginCall call) { work.execute(() -> {
        try { JSONObject state = state(); if (state == null) throw new Exception("请先连接电脑");
            call.resolve(send(state, call.getString("method", "GET"), call.getString("path", ""), call.getString("body"), true));
        } catch (Exception e) { call.reject("连接未完成，请检查电脑是否开机、网络是否可达，以及配对身份是否一致"); }
    }); }

    @PluginMethod public void forget(PluginCall call) { work.execute(() -> {
        if (getContext().getSharedPreferences(KEY, Context.MODE_PRIVATE).edit().clear().commit()) call.resolve();
        else call.reject("无法移除连接，请重试");
    }); }
}
