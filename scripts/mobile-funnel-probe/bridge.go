package main

import (
	"bytes"
	"crypto/tls"
	"encoding/json"
	"errors"
	"io"
	"log"
	"net"
	"net/http"
	"net/http/httputil"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

// Developer integration only. The target is a dedicated loopback HTTPS listener,
// never the desktop server. Both TLS hops verify the public hostname.
type bridgeConfig struct {
	Origin   string `json:"origin"`
	Upstream string `json:"upstream"`
}

func readBridge(path, origin string) (bridgeConfig, error) {
	var c bridgeConfig
	info, err := os.Lstat(path)
	if err != nil || !filepath.IsAbs(path) || !info.Mode().IsRegular() || info.Mode().Perm()&0077 != 0 || info.Size() > 4096 {
		return c, errors.New("mobile bridge config must be a private, small regular file")
	}
	f, err := os.Open(path)
	if err != nil {
		return c, err
	}
	defer f.Close()
	d := json.NewDecoder(io.LimitReader(f, 4097))
	d.DisallowUnknownFields()
	if err := d.Decode(&c); err != nil {
		return c, errors.New("invalid mobile bridge config")
	}
	var extra any
	if d.Decode(&extra) != io.EOF {
		return c, errors.New("unexpected trailing bridge config")
	}
	u, err := url.Parse(c.Upstream)
	if err != nil {
		return c, errors.New("invalid mobile bridge target")
	}
	ip := net.ParseIP(u.Hostname())
	port, err := strconv.Atoi(u.Port())
	if c.Origin != origin || u.Scheme != "https" || ip == nil || !ip.IsLoopback() ||
		err != nil || port < 1 || port > 65535 || u.User != nil || u.Path != "" || u.RawQuery != "" || u.ForceQuery || u.Fragment != "" {
		return c, errors.New("mobile bridge requires matching origin and loopback HTTPS target")
	}
	return c, nil
}

func mobilePath(path string) bool {
	if strings.ContainsAny(path, "\\\x00") {
		return false
	}
	for _, segment := range strings.Split(path, "/") {
		if segment == "." || segment == ".." {
			return false
		}
	}
	return path == "/mobile" || strings.HasPrefix(path, "/mobile/") || path == "/phone" || strings.HasPrefix(path, "/phone/")
}

func mobileBridge(c bridgeConfig, transport *http.Transport) http.Handler {
	target, _ := url.Parse(c.Upstream) // Validated by readBridge before startup.
	public, _ := url.Parse(c.Origin)
	proxy := &httputil.ReverseProxy{
		Transport: transport,
		Rewrite: func(r *httputil.ProxyRequest) {
			r.SetURL(target)
			r.Out.Host = public.Host
			// Never let caller-supplied forwarding/replay hints alter the target.
			for _, name := range []string{"Forwarded", "X-Forwarded-For", "X-Forwarded-Host", "X-Forwarded-Proto", "X-Real-IP", "Idempotency-Key", "X-Idempotency-Key"} {
				r.Out.Header.Del(name)
			}
		},
		ErrorLog: log.New(io.Discard, "", 0),
		ErrorHandler: func(w http.ResponseWriter, _ *http.Request, _ error) {
			http.Error(w, "Computer mobile service unavailable", http.StatusBadGateway)
		},
	}
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Cache-Control", "no-store")
		w.Header().Set("Referrer-Policy", "no-referrer")
		w.Header().Set("X-Content-Type-Options", "nosniff")
		w.Header().Set("X-Frame-Options", "DENY")
		if r.TLS == nil || (r.Host != public.Host && r.Host != public.Host+":443") ||
			(r.Header.Get("Origin") != "" && r.Header.Get("Origin") != c.Origin) || r.Header.Get("Sec-Fetch-Site") == "cross-site" {
			http.Error(w, "Forbidden", http.StatusForbidden)
			return
		}
		if r.URL.Path == "/" && r.Method == "GET" && r.URL.RawQuery == "" {
			http.Redirect(w, r, "/mobile/", http.StatusTemporaryRedirect)
			return
		}
		if !mobilePath(r.URL.Path) {
			http.NotFound(w, r)
			return
		}
		if r.Method != "GET" && r.Method != "HEAD" && r.Method != "POST" || r.Header.Get("Upgrade") != "" {
			http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
			return
		}
		// Buffer only the bounded request body. No retries and no content logging.
		body, err := io.ReadAll(http.MaxBytesReader(w, r.Body, 64*1024))
		if err != nil {
			http.Error(w, "Request too large or unreadable", http.StatusRequestEntityTooLarge)
			return
		}
		r.Body = io.NopCloser(bytes.NewReader(body))
		r.ContentLength = int64(len(body))
		proxy.ServeHTTP(w, r)
	})
}

func bridgeTransport(origin string) *http.Transport {
	u, _ := url.Parse(origin)
	return &http.Transport{
		// No environment proxy on this computer-local hop; validate with system CAs.
		Proxy: nil, TLSClientConfig: &tls.Config{MinVersion: tls.VersionTLS12, ServerName: u.Hostname()},
		DialContext:         (&net.Dialer{Timeout: 3 * time.Second}).DialContext,
		TLSHandshakeTimeout: 5 * time.Second, ResponseHeaderTimeout: 20 * time.Second,
		DisableKeepAlives: true, // A failed connection must not replay an Agent POST.
	}
}
