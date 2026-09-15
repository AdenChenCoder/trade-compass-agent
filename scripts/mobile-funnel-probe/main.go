// Developer Funnel validation: synthetic content by default, explicit mobile-only integration.
package main

import (
	"context"
	"crypto/rand"
	"crypto/tls"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"net/url"
	"os"
	"os/signal"
	"path/filepath"
	"regexp"
	"strings"
	"syscall"
	"time"

	"tailscale.com/envknob"
	"tailscale.com/ipn"
	"tailscale.com/tsnet"
	tsversion "tailscale.com/version"
)

type identity struct {
	Hostname string `json:"hostname"`
	Origin   string `json:"origin,omitempty"`
}

type status struct {
	Phase       string              `json:"phase"`
	Origin      string              `json:"origin,omitempty"`
	ActionURL   string              `json:"action_url,omitempty"`
	UpdatedAt   time.Time           `json:"updated_at"`
	Instance    string              `json:"instance,omitempty"`
	Diagnostics *diagnosticSnapshot `json:"diagnostics,omitempty"`
}

type runOptions struct {
	bridge        string
	waitForMobile bool
	instance      string
}

func writeJSON(path string, value any) error {
	data, err := json.MarshalIndent(value, "", "  ")
	if err != nil {
		return err
	}
	f, err := os.CreateTemp(filepath.Dir(path), ".probe-*")
	if err != nil {
		return err
	}
	defer os.Remove(f.Name())
	if _, err = f.Write(data); err != nil {
		f.Close()
		return err
	}
	if err = f.Sync(); err != nil {
		f.Close()
		return err
	}
	if err = f.Close(); err != nil {
		return err
	}
	return os.Rename(f.Name(), path)
}

func privateDirectory(directory string) error {
	if !filepath.IsAbs(directory) {
		return errors.New("state-dir must be an absolute writable directory outside the package")
	}
	if err := os.MkdirAll(directory, 0700); err != nil {
		return err
	}
	info, err := os.Lstat(directory)
	if err != nil || !info.IsDir() || info.Mode()&os.ModeSymlink != 0 {
		return errors.New("state-dir must be a real directory")
	}
	return os.Chmod(directory, 0700)
}

func prepare(directory string) (identity, error) {
	var id identity
	if err := privateDirectory(directory); err != nil {
		return id, err
	}
	path := filepath.Join(directory, "identity.json")
	if info, err := os.Lstat(path); err == nil && (!info.Mode().IsRegular() || info.Mode().Perm()&0077 != 0) {
		return id, errors.New("identity must be a private regular file")
	}
	data, err := os.ReadFile(path)
	if err == nil {
		if json.Unmarshal(data, &id) != nil || !regexp.MustCompile(`^compass-probe-[a-f0-9]{16}$`).MatchString(id.Hostname) {
			return id, errors.New("invalid identity; preserve state and investigate before replacing it")
		}
		return id, nil
	}
	if !errors.Is(err, os.ErrNotExist) {
		return id, err
	}
	b := make([]byte, 8)
	if _, err := rand.Read(b); err != nil {
		return id, err
	}
	id.Hostname = "compass-probe-" + hex.EncodeToString(b)
	return id, writeJSON(path, id)
}

// Only provider-owned HTTPS actions can be offered to the operator.
func actionURL(raw string) string {
	u, err := url.Parse(raw)
	if err != nil || u.Scheme != "https" || u.User != nil || u.Port() != "" {
		return ""
	}
	host := u.Hostname()
	if host != "login.tailscale.com" && host != "controlplane.tailscale.com" {
		return ""
	}
	return raw
}

func probeHandler(host string) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Cache-Control", "no-store")
		w.Header().Set("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
		w.Header().Set("X-Content-Type-Options", "nosniff")
		w.Header().Set("Referrer-Policy", "no-referrer")
		w.Header().Set("X-Frame-Options", "DENY")
		if r.TLS == nil || (r.Host != host && r.Host != host+":443") {
			http.Error(w, "Forbidden", http.StatusForbidden)
			return
		}
		if r.Method != "GET" && r.Method != "HEAD" {
			w.Header().Set("Allow", "GET, HEAD")
			http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
			return
		}
		if r.URL.RawQuery != "" || (r.URL.Path != "/" && r.URL.Path != "/healthz") {
			http.NotFound(w, r)
			return
		}
		w.Header().Set("Content-Type", "text/plain; charset=utf-8")
		if r.Method == "GET" {
			io.WriteString(w, "Trade Compass：加密连接验证页面。\n此入口仅返回固定测试文字，不连接 Agent、会话或任务。\n")
		}
	})
}

func run(ctx context.Context, directory string, connect bool, options ...runOptions) error {
	var opts runOptions
	if len(options) > 0 {
		opts = options[0]
	}
	if err := privateDirectory(directory); err != nil {
		return err
	}
	unlock, err := lockState(directory)
	if err != nil {
		return err
	}
	defer unlock()
	id, err := prepare(directory)
	if err != nil {
		return err
	}
	lastPhase := ""
	diagnostics := &connectionDiagnostics{}
	publish := func(phase, origin, action string) error {
		value := status{Phase: phase, Origin: origin, ActionURL: actionURL(action), UpdatedAt: time.Now().UTC(), Instance: opts.instance}
		if connect {
			snapshot := diagnostics.snapshot()
			value.Diagnostics = &snapshot
		}
		if err := writeJSON(filepath.Join(directory, "status.json"), value); err != nil {
			return err
		}
		if phase != lastPhase {
			fmt.Println(phase)
			lastPhase = phase
		}
		return nil
	}
	if !connect {
		return publish("prepared_no_network", id.Origin, "")
	}
	// No inherited identity, alternate control plane or credentials from another task.
	for _, entry := range os.Environ() {
		key, _, _ := strings.Cut(entry, "=")
		if strings.HasPrefix(key, "TS_") || strings.HasPrefix(key, "TSNET_") {
			return errors.New("remove inherited TS_/TSNET_ environment overrides before connecting")
		}
	}
	envknob.SetNoLogsNoSupport()
	envknob.Setenv("TS_DISABLE_PORTMAPPER", "true")
	if err := publish("starting", id.Origin, ""); err != nil {
		return err
	}
	defer func() { _ = publish("stopped", id.Origin, "") }()
	srv := &tsnet.Server{Dir: filepath.Join(directory, "tsnet"), Hostname: id.Hostname,
		UserLogf: func(string, ...any) {}, Logf: diagnostics.logf}
	if err := srv.Start(); err != nil {
		return errors.New("Tailscale startup failed; no Agent endpoint was exposed")
	}
	defer srv.Close()
	lc, err := srv.LocalClient()
	if err != nil {
		return errors.New("local Tailscale API unavailable")
	}
	var server *http.Server
	var funnel net.Listener
	closeListener := func() {
		if server != nil {
			server.Close()
			server = nil
		}
		if funnel != nil {
			funnel.Close()
			funnel = nil
		}
	}
	defer closeListener()
	serveErr := make(chan error, 1)
	var lastFeatureQuery time.Time
	featureURL := ""
	ticker := time.NewTicker(2 * time.Second)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return nil
		case <-serveErr:
			return errors.New("probe listener stopped unexpectedly")
		case <-ticker.C:
		}
		checkCtx, cancel := context.WithTimeout(ctx, 8*time.Second)
		st, err := lc.StatusWithoutPeers(checkCtx)
		var sc *ipn.ServeConfig
		configRead := false
		if err == nil && id.Origin != "" {
			var configErr error
			sc, configErr = lc.GetServeConfig(checkCtx)
			configRead = configErr == nil
		}
		cancel()
		diagnostics.control(st, sc, configRead, strings.TrimPrefix(id.Origin, "https://"), server != nil)
		if err != nil {
			closeListener()
			if err := publish("control_status_unavailable", id.Origin, ""); err != nil {
				return err
			}
			continue
		}
		if st.BackendState != "Running" || st.Self == nil {
			closeListener()
			if err := publish("needs_login_or_network", id.Origin, st.AuthURL); err != nil {
				return err
			}
			continue
		}
		if len(st.CertDomains) == 0 || ipn.CheckFunnelAccess(443, st.Self) != nil {
			closeListener()
			if time.Since(lastFeatureQuery) >= 30*time.Second {
				lastFeatureQuery = time.Now()
				featureCtx, cancel := context.WithTimeout(ctx, 8*time.Second)
				feature, err := lc.QueryFeature(featureCtx, "funnel")
				cancel()
				if err == nil {
					featureURL = feature.URL
				}
			}
			if err := publish("needs_funnel_https_permission", id.Origin, featureURL); err != nil {
				return err
			}
			continue
		}
		host := strings.TrimSuffix(st.Self.DNSName, ".")
		if !regexp.MustCompile(`^[a-z0-9-]+\.[a-z0-9.-]+\.ts\.net$`).MatchString(host) {
			return errors.New("unexpected certificate domain")
		}
		matchesCertificate := false
		for _, domain := range st.CertDomains {
			if strings.TrimSuffix(domain, ".") == host {
				matchesCertificate = true
			}
		}
		if !matchesCertificate {
			return errors.New("certificate domain does not match this node")
		}
		origin := "https://" + host
		if id.Origin != "" && id.Origin != origin {
			return errors.New("public origin changed; review before creating a new phone installation")
		}
		if server == nil {
			handler := probeHandler(host)
			// Provision before accepting a browser handshake; first issuance can
			// exceed the HTTP server's handshake timeout. Keep private keys local.
			if err := publish("preparing_certificate", origin, ""); err != nil {
				return err
			}
			certCtx, cancel := context.WithTimeout(ctx, 45*time.Second)
			certPEM, keyPEM, err := lc.CertPair(certCtx, host)
			cancel()
			if err != nil {
				return errors.New("public certificate preparation failed; preserve state and check connectivity")
			}
			if _, err := tls.X509KeyPair(certPEM, keyPEM); err != nil {
				return errors.New("public certificate and key do not match")
			}
			if id.Origin == "" {
				id.Origin = origin
				if err := writeJSON(filepath.Join(directory, "identity.json"), id); err != nil {
					return err
				}
			}
			if opts.bridge != "" {
				if _, err := os.Lstat(opts.bridge); errors.Is(err, os.ErrNotExist) && opts.waitForMobile {
					if err := publish("waiting_for_mobile", origin, ""); err != nil {
						return err
					}
					continue
				}
				bridge, err := readBridge(opts.bridge, origin)
				if err != nil {
					return err
				}
				handler = mobileBridge(bridge, bridgeTransport(origin))
			}
			ln, err := srv.ListenFunnel("tcp", ":443", tsnet.FunnelOnly(), tsnet.FunnelTLSConfig(&tls.Config{
				MinVersion: tls.VersionTLS12, GetCertificate: diagnostics.certificate(lc.GetCertificate),
			}))
			if err != nil {
				return errors.New("Funnel listener could not start")
			}
			funnel = ln
			server = &http.Server{Handler: diagnostics.handler(handler), ReadHeaderTimeout: 5 * time.Second,
				ReadTimeout: 10 * time.Second, WriteTimeout: 30 * time.Second, IdleTimeout: 30 * time.Second,
				MaxHeaderBytes: 16 * 1024, ErrorLog: log.New(diagnostics, "", 0)}
			go func(s *http.Server) {
				if err := s.Serve(ln); err != nil && !errors.Is(err, http.ErrServerClosed) && !errors.Is(err, net.ErrClosed) {
					serveErr <- err
				}
			}(server)
		}
		// A listener is not evidence of public reachability; a phone must check it.
		phase := "listening_public_access_unverified"
		if opts.bridge != "" {
			phase = "listening_mobile_access_unverified"
		}
		if err := publish(phase, id.Origin, ""); err != nil {
			return err
		}
	}
}

func main() {
	version := flag.Bool("version", false, "Print the bundled helper protocol")
	directory := flag.String("state-dir", "", "Private, persistent probe directory outside the package")
	connect := flag.Bool("connect", false, "Contact Tailscale and, after authorization, expose synthetic probe content")
	bridge := flag.String("mobile-bridge-config", "", "Explicit private configuration for the mobile-only HTTPS integration test")
	waitMobile := flag.Bool("wait-for-mobile", false, "Wait for the parent to prepare its private mobile listener")
	parentInput := flag.Bool("parent-stdin", false, "Close the public listener when the parent pipe closes")
	instance := flag.String("instance", "", "Parent startup identifier for fresh status records")
	flag.Parse()
	if *version {
		_ = json.NewEncoder(os.Stdout).Encode(map[string]any{
			"name": "compass-connect", "protocol": 1, "tailscale_version": tsversion.Long(),
		})
		return
	}
	if *waitMobile && (*bridge == "" || !*parentInput || !regexp.MustCompile(`^[a-f0-9]{32}$`).MatchString(*instance)) {
		fmt.Fprintln(os.Stderr, "managed mode requires a bridge, parent pipe and startup identifier")
		os.Exit(1)
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	if *parentInput {
		stopWhenParentCloses(os.Stdin, stop)
	}
	if err := run(ctx, *directory, *connect, runOptions{*bridge, *waitMobile, *instance}); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}

func stopWhenParentCloses(input io.Reader, stop context.CancelFunc) {
	go func() { _, _ = io.Copy(io.Discard, input); stop() }()
}
