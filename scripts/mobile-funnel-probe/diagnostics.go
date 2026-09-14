package main

import (
	"crypto/tls"
	"net/http"
	"strings"
	"sync"
	"time"

	"tailscale.com/ipn"
	"tailscale.com/ipn/ipnstate"
)

// Diagnostics deliberately contain no raw provider logs, URLs, peers or errors.
// They describe this process only and are stored in its private status file.
type controlDiagnostic struct {
	StatusAvailable  bool `json:"status_available"`
	Online           bool `json:"online"`
	FunnelPermission bool `json:"funnel_permission"`
	ServeConfigRead  bool `json:"serve_config_read"`
	FunnelConfigured bool `json:"funnel_configured"`
	ListenerOpen     bool `json:"listener_open"`
	HealthIssues     int  `json:"health_issues"`
}

type diagnosticEvent struct {
	Code string    `json:"code"`
	At   time.Time `json:"at"`
}

type diagnosticSnapshot struct {
	Control  controlDiagnostic `json:"control"`
	Counters map[string]uint64 `json:"counters"`
	Recent   []diagnosticEvent `json:"recent,omitempty"`
}

type connectionDiagnostics struct {
	mu    sync.Mutex
	state diagnosticSnapshot
}

func (d *connectionDiagnostics) count(code string, recent bool) {
	d.mu.Lock()
	defer d.mu.Unlock()
	if d.state.Counters == nil {
		d.state.Counters = make(map[string]uint64)
	}
	d.state.Counters[code]++
	if recent {
		d.eventLocked(code)
	}
}

func (d *connectionDiagnostics) eventLocked(code string) {
	d.state.Recent = append(d.state.Recent, diagnosticEvent{code, time.Now().UTC()})
	if len(d.state.Recent) > 8 {
		d.state.Recent = d.state.Recent[len(d.state.Recent)-8:]
	}
}

func (d *connectionDiagnostics) control(st *ipnstate.Status, sc *ipn.ServeConfig, read bool, host string, listening bool) {
	value := controlDiagnostic{ServeConfigRead: read, ListenerOpen: listening}
	if st != nil {
		value.StatusAvailable = true
		value.HealthIssues = len(st.Health)
		if st.Self != nil {
			value.Online = st.Self.Online
			value.FunnelPermission = ipn.CheckFunnelAccess(443, st.Self) == nil
		}
	}
	if read && sc != nil && host != "" {
		value.FunnelConfigured = sc.AllowFunnel[ipn.HostPort(host+":443")]
	}
	d.mu.Lock()
	defer d.mu.Unlock()
	if d.state.Control != value {
		d.state.Control = value
		d.eventLocked("control_changed")
	}
}

func (d *connectionDiagnostics) snapshot() diagnosticSnapshot {
	d.mu.Lock()
	defer d.mu.Unlock()
	value := d.state
	value.Counters = make(map[string]uint64, len(d.state.Counters))
	for key, count := range d.state.Counters {
		value.Counters[key] = count
	}
	value.Recent = append([]diagnosticEvent(nil), d.state.Recent...)
	return value
}

// Match only known fixed messages from the pinned provider source. Never format
// or persist arguments, which can contain addresses, login URLs and credentials.
func (d *connectionDiagnostics) logf(format string, _ ...any) {
	switch {
	case strings.Contains(format, "ingress: denied; no ingress cap from"):
		d.count("ingress_permission_denied", true)
	case strings.Contains(format, "got ingress conn w/o serveConfig; rejecting"):
		d.count("ingress_config_missing", true)
	case strings.Contains(format, "got ingress conn for unconfigured"):
		d.count("ingress_target_missing", true)
	}
}

// http.Server emits arbitrary error text. Only retain the error category.
func (d *connectionDiagnostics) Write(data []byte) (int, error) {
	if strings.HasPrefix(string(data), "http: TLS handshake error") {
		d.count("tls_handshake_failed", true)
	}
	return len(data), nil
}

func (d *connectionDiagnostics) certificate(get func(*tls.ClientHelloInfo) (*tls.Certificate, error)) func(*tls.ClientHelloInfo) (*tls.Certificate, error) {
	return func(hello *tls.ClientHelloInfo) (*tls.Certificate, error) {
		d.count("tls_client_hello", false)
		cert, err := get(hello)
		if err != nil {
			d.count("tls_certificate_failed", true)
		}
		return cert, err
	}
}

func (d *connectionDiagnostics) handler(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		d.count("http_request", false)
		next.ServeHTTP(w, r)
	})
}
