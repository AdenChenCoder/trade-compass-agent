package main

import (
	"crypto/tls"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"

	"tailscale.com/ipn"
	"tailscale.com/ipn/ipnstate"
)

func TestDiagnosticsKeepOnlyBoundedCategories(t *testing.T) {
	d := &connectionDiagnostics{}
	var wg sync.WaitGroup
	for range 20 {
		wg.Go(func() {
			for range 100 {
				d.logf("peerapi: ingress: denied; no ingress cap from %v", "private-address")
				d.logf("handleIngress: got ingress conn w/o serveConfig; rejecting")
				d.logf("handleIngress: got ingress conn for unconfigured %q; rejecting", "private-host")
				d.logf("login URL: %s", "https://login.tailscale.com/private-token")
				d.Write([]byte("http: TLS handshake error from private-address: private-error\n"))
				_ = d.snapshot()
			}
		})
	}
	wg.Wait()
	snapshot := d.snapshot()
	for _, code := range []string{"ingress_permission_denied", "ingress_config_missing", "ingress_target_missing", "tls_handshake_failed"} {
		if snapshot.Counters[code] != 2000 {
			t.Fatalf("missing concurrent events: %s", code)
		}
	}
	data, err := json.Marshal(snapshot)
	if err != nil || len(data) > 4096 || len(snapshot.Recent) != 8 || strings.Contains(string(data), "private-") {
		t.Fatalf("unsafe or unbounded diagnostic snapshot: size=%d", len(data))
	}
	// Exported snapshots must not share mutable state with the collector.
	snapshot.Counters["ingress_permission_denied"] = 0
	snapshot.Recent[0].Code = "private-mutated"
	if d.snapshot().Counters["ingress_permission_denied"] != 2000 || d.snapshot().Recent[0].Code == "private-mutated" {
		t.Fatal("snapshot aliases internal state")
	}
}

func TestDiagnosticsDistinguishControlConfigAndListener(t *testing.T) {
	d := &connectionDiagnostics{}
	st := &ipnstate.Status{Self: &ipnstate.PeerStatus{Online: true}, Health: []string{"private-health-warning"}}
	sc := &ipn.ServeConfig{AllowFunnel: map[ipn.HostPort]bool{"private-host.ts.net:443": true}}
	d.control(st, sc, true, "private-host.ts.net", true)
	first := d.snapshot()
	if !first.Control.Online || !first.Control.FunnelConfigured || !first.Control.ListenerOpen || first.Control.HealthIssues != 1 {
		t.Fatal("did not capture the current node's operational state")
	}
	d.control(st, sc, true, "private-host.ts.net", true)
	if len(d.snapshot().Recent) != len(first.Recent) {
		t.Fatal("unchanged control state floods the event buffer")
	}
	d.control(nil, nil, false, "private-host.ts.net", true)
	last := d.snapshot()
	if last.Control.StatusAvailable || last.Control.ServeConfigRead || last.Control.Online || !last.Control.ListenerOpen {
		t.Fatal("API failure reused stale control evidence")
	}
	data, _ := json.Marshal(first)
	if strings.Contains(string(data), "private-") {
		t.Fatal("diagnostics leaked provider details")
	}
}

func TestDiagnosticsPreserveCertificateFailures(t *testing.T) {
	d := &connectionDiagnostics{}
	problem := errors.New("private-certificate-error")
	get := d.certificate(func(*tls.ClientHelloInfo) (*tls.Certificate, error) { return nil, problem })
	cert, err := get(&tls.ClientHelloInfo{ServerName: "private-host.ts.net"})
	if cert != nil || err != problem {
		t.Fatal("diagnostics changed the certificate failure")
	}
	value := d.snapshot()
	if value.Counters["tls_client_hello"] != 1 || value.Counters["tls_certificate_failed"] != 1 {
		t.Fatal("certificate failure not distinguished from ingress failure")
	}
	data, _ := json.Marshal(value)
	if strings.Contains(string(data), "private-") {
		t.Fatal("certificate diagnostic leaked an error or hostname")
	}
}

func TestHTTPSDiagnosticsPreservePublicSurface(t *testing.T) {
	d := &connectionDiagnostics{}
	server := httptest.NewUnstartedServer(d.handler(probeHandler("probe.example.ts.net")))
	server.TLS = &tls.Config{}
	server.StartTLS()
	defer server.Close()
	for path, expected := range map[string]int{"/healthz": 200, "/diagnostics.json": 404, "/status.json": 404, "/api/config": 404} {
		request, err := http.NewRequest("GET", server.URL+path, nil)
		if err != nil {
			t.Fatal(err)
		}
		request.Host = "probe.example.ts.net"
		response, err := server.Client().Do(request)
		if err != nil {
			t.Fatal(err)
		}
		io.Copy(io.Discard, response.Body)
		response.Body.Close()
		if response.StatusCode != expected {
			t.Fatalf("public surface changed: %s returned %d", path, response.StatusCode)
		}
	}
	if d.snapshot().Counters["http_request"] != 4 {
		t.Fatal("TLS requests did not reach the application handler")
	}
}
