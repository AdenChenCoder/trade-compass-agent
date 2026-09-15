package main

import (
	"context"
	"crypto/tls"
	"io"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestParentPipeClosureCancelsConnection(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	reader, writer := io.Pipe()
	defer reader.Close()
	stopWhenParentCloses(reader, cancel)
	if ctx.Err() != nil {
		t.Fatal("cancelled before parent exit")
	}
	writer.Close()
	select {
	case <-ctx.Done():
	case <-time.After(time.Second):
		t.Fatal("connection survived parent exit")
	}
}

func TestPrivateIdentitySurvivesPreparation(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "state")
	first, err := prepare(dir)
	if err != nil {
		t.Fatal(err)
	}
	second, err := prepare(dir)
	if err != nil || first != second {
		t.Fatalf("identity changed: %v", err)
	}
	for _, path := range []string{dir, filepath.Join(dir, "identity.json")} {
		info, err := os.Stat(path)
		if err != nil || info.Mode().Perm()&0077 != 0 {
			t.Fatalf("not private: %s", path)
		}
	}
	if err := run(context.Background(), dir, false); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(filepath.Join(dir, "tsnet")); !os.IsNotExist(err) {
		t.Fatal("prepare initialized provider state")
	}
}

func TestInvalidStateIsPreserved(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "identity.json")
	if err := os.WriteFile(path, []byte("not-json"), 0600); err != nil {
		t.Fatal(err)
	}
	if _, err := prepare(dir); err == nil {
		t.Fatal("accepted corrupt identity")
	}
	data, _ := os.ReadFile(path)
	if string(data) != "not-json" {
		t.Fatal("corrupt state was overwritten")
	}
	link := filepath.Join(t.TempDir(), "link")
	if err := os.Symlink(dir, link); err != nil {
		t.Fatal(err)
	}
	if _, err := prepare(link); err == nil {
		t.Fatal("accepted linked state directory")
	}
}

func TestActionURLsStayOnProvider(t *testing.T) {
	for _, raw := range []string{"http://login.tailscale.com/a", "https://evil.example/a", "https://login.tailscale.com.evil.example/a", "https://user@login.tailscale.com/a", "https://login.tailscale.com:444/a", "//login.tailscale.com/a"} {
		if actionURL(raw) != "" {
			t.Fatalf("accepted %s", raw)
		}
	}
	url := "https://login.tailscale.com/a/test"
	if actionURL(url) != url {
		t.Fatal("provider action rejected")
	}
}

func TestConcurrentStateUseIsRejected(t *testing.T) {
	dir := t.TempDir()
	unlock, err := lockState(dir)
	if err != nil {
		t.Fatal(err)
	}
	if other, err := lockState(dir); err == nil {
		other()
		t.Fatal("two processes could use the same identity")
	}
	unlock()
	again, err := lockState(dir)
	if err != nil {
		t.Fatal(err)
	}
	again()
}

func TestSyntheticPublicSurface(t *testing.T) {
	host := "compass-probe.example.ts.net"
	for _, c := range []struct {
		method, path, host string
		secure             bool
		want               int
	}{
		{"GET", "/", host, true, 200}, {"GET", "/healthz", host, true, 200},
		{"GET", "/api/config", host, true, 404}, {"GET", "/mobile/v1/sessions", host, true, 404},
		{"GET", "/identity.json", host, true, 404}, {"GET", "/status.json", host, true, 404},
		{"GET", "/?token=secret", host, true, 404}, {"POST", "/", host, true, 405},
		{"GET", "/", "evil.example", true, 403}, {"GET", "/", host, false, 403},
	} {
		r := httptest.NewRequest(c.method, "http://"+c.host+c.path, nil)
		if c.secure {
			r.TLS = &tls.ConnectionState{}
		}
		r.Header.Set("X-Forwarded-Proto", "https")
		w := httptest.NewRecorder()
		probeHandler(host).ServeHTTP(w, r)
		if w.Code != c.want {
			t.Fatalf("%s %s: got %d want %d", c.method, c.path, w.Code, c.want)
		}
		if w.Header().Get("Cache-Control") != "no-store" || w.Header().Get("Content-Security-Policy") == "" {
			t.Fatal("missing security headers")
		}
	}
}

func TestRealHTTPSWithVerifiedTestCA(t *testing.T) {
	host := "probe.example.ts.net"
	server := httptest.NewTLSServer(probeHandler(host))
	defer server.Close()
	client := server.Client() // Trust this test server's certificate; no InsecureSkipVerify.
	request := httptest.NewRequest("GET", server.URL+"/healthz", nil)
	request.RequestURI = ""
	request.Host = host
	response, err := client.Do(request)
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	body, err := io.ReadAll(response.Body)
	if err != nil || response.StatusCode != 200 || len(body) == 0 {
		t.Fatal("TLS consumer check failed")
	}
}
