package main

import (
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"io"
	"log"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
)

func TestBridgeRejectsUnsafeConfiguration(t *testing.T) {
	file := filepath.Join(t.TempDir(), "bridge.json")
	origin := "https://computer.example"
	for _, target := range []string{"http://127.0.0.1:8000", "https://localhost:8000", "https://192.168.1.1:8000", "https://example.com:8000", "https://127.0.0.1", "https://127.0.0.1:0", "https://127.0.0.1:65536", "https://user@127.0.0.1:8000", "https://127.0.0.1:8000/path", "https://127.0.0.1:8000?", "https://127.0.0.1:8000#fragment"} {
		data, _ := json.Marshal(bridgeConfig{origin, target})
		os.WriteFile(file, data, 0600)
		if _, err := readBridge(file, origin); err == nil {
			t.Fatalf("accepted %s", target)
		}
	}
	data, _ := json.Marshal(bridgeConfig{origin, "https://127.0.0.1:8000"})
	os.WriteFile(file, data, 0600)
	if _, err := readBridge(file, origin); err != nil {
		t.Fatal(err)
	}
	if _, err := readBridge(file, "https://another.example"); err == nil {
		t.Fatal("accepted different public origin")
	}
	os.Chmod(file, 0644)
	if _, err := readBridge(file, origin); err == nil {
		t.Fatal("accepted public configuration")
	}
}

func TestBridgeHTTPSBoundaryAndNoReplay(t *testing.T) {
	var calls atomic.Int32
	upstream := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		if r.TLS == nil || r.Host != "example.com" || r.Header.Get("X-Forwarded-Proto") != "" || r.Header.Get("Idempotency-Key") != "" {
			t.Error("incorrect TLS/host/forwarding boundary")
		}
		if r.Header.Get("Cookie") != "__Host-compass-device=test-secret" || r.Header.Get("X-Compass-PWA") != "1" {
			t.Error("device authentication headers changed")
		}
		body, _ := io.ReadAll(r.Body)
		w.Header().Set("Set-Cookie", "__Host-compass-device=reply; Secure; HttpOnly; SameSite=Strict; Path=/")
		w.WriteHeader(http.StatusAccepted)
		w.Write(body)
	}))
	defer upstream.Close()
	upstream.Config.ErrorLog = log.New(io.Discard, "", 0)
	c := bridgeConfig{"https://example.com", upstream.URL}
	transport := bridgeTransport(c.Origin)
	transport.TLSClientConfig.RootCAs = upstream.Client().Transport.(*http.Transport).TLSClientConfig.RootCAs
	handler := mobileBridge(c, transport)
	request := func(method, path string) *http.Request {
		r := httptest.NewRequest(method, c.Origin+path, strings.NewReader(`{"request_id":"one-request"}`))
		r.TLS = &tls.ConnectionState{}
		r.Header.Set("Cookie", "__Host-compass-device=test-secret")
		r.Header.Set("X-Compass-PWA", "1")
		r.Header.Set("Origin", c.Origin)
		r.Header.Set("X-Forwarded-Proto", "http")
		r.Header.Set("Idempotency-Key", "must-not-replay")
		return r
	}
	w := httptest.NewRecorder()
	handler.ServeHTTP(w, request("POST", "/mobile/v1/turns"))
	if w.Code != 202 || !strings.Contains(w.Body.String(), "one-request") || !strings.Contains(w.Header().Get("Set-Cookie"), "HttpOnly") || calls.Load() != 1 {
		t.Fatalf("verified HTTPS bridge failed: %d %s, calls=%d", w.Code, w.Body.String(), calls.Load())
	}
	for _, path := range []string{"/api/config", "/api/mobile/devices", "/agent", "/docs", "/openapi.json", "/identity.json", "/mobile/../api/config", "/mobile/%2e%2e/api/config", "/mobile\\api", "/mobileevil/"} {
		w = httptest.NewRecorder()
		handler.ServeHTTP(w, request("GET", path))
		if w.Code != 404 || calls.Load() != 1 {
			t.Fatalf("unsafe route forwarded: %s", path)
		}
	}
	for _, mutate := range []func(*http.Request){
		func(r *http.Request) { r.TLS = nil },
		func(r *http.Request) { r.Host = "evil.example" },
		func(r *http.Request) { r.Header.Set("Origin", "https://evil.example") },
		func(r *http.Request) { r.Header.Set("Sec-Fetch-Site", "cross-site") },
	} {
		r := request("GET", "/mobile/")
		mutate(r)
		w = httptest.NewRecorder()
		handler.ServeHTTP(w, r)
		if w.Code != 403 || calls.Load() != 1 {
			t.Fatal("invalid transport/origin reached backend")
		}
	}
	r := request("POST", "/mobile/v1/turns")
	r.Body = io.NopCloser(strings.NewReader(strings.Repeat("x", 65537)))
	w = httptest.NewRecorder()
	handler.ServeHTTP(w, r)
	if w.Code != 413 || calls.Load() != 1 {
		t.Fatal("oversized message forwarded")
	}
	// An untrusted certificate must fail before credentials or body reach the target.
	transport.TLSClientConfig.RootCAs = x509.NewCertPool()
	w = httptest.NewRecorder()
	handler.ServeHTTP(w, request("POST", "/mobile/v1/turns"))
	if w.Code != 502 || calls.Load() != 1 || strings.Contains(w.Body.String(), "secret") {
		t.Fatal("invalid upstream certificate accepted or leaked diagnostics")
	}
	transport.TLSClientConfig.RootCAs = upstream.Client().Transport.(*http.Transport).TLSClientConfig.RootCAs
	transport.TLSClientConfig.ServerName = "wrong.example"
	w = httptest.NewRecorder()
	handler.ServeHTTP(w, request("POST", "/mobile/v1/turns"))
	if w.Code != 502 || calls.Load() != 1 {
		t.Fatal("wrong upstream hostname accepted")
	}
}
