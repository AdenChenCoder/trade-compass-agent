package main

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"tailscale.com/tempfork/acme"
)

func TestCertificateLimitSurvivesRestart(t *testing.T) {
	now := time.Now().UTC()
	problem := &acme.Error{ProblemType: "urn:ietf:params:acme:error:rateLimited", Detail: "private-domain private-token", Header: http.Header{"Retry-After": []string{"7200"}}}
	failure := nextCertificateFailure(problem, certificateFailure{}, now)
	if failure.RetryAt.Before(now.Add(2*time.Hour)) || failure.Code != "certificate_acme_rateLimited" {
		t.Fatal("provider limit was shortened")
	}
	path := filepath.Join(t.TempDir(), "certificate-retry.json")
	if err := writeJSON(path, failure); err != nil {
		t.Fatal(err)
	}
	restored, err := readCertificateFailure(path)
	if err != nil || !restored.RetryAt.Equal(failure.RetryAt) || restored.Attempts != 1 {
		t.Fatal("restart bypassed cooldown")
	}
	data, _ := os.ReadFile(path)
	if strings.Contains(string(data), "private-") || strings.Contains(string(data), "https:") {
		t.Fatal("persisted provider credentials")
	}
	info, _ := os.Stat(path)
	if info.Mode().Perm()&0077 != 0 {
		t.Fatal("retry state is not private")
	}
}

func TestCertificateFailureBackoff(t *testing.T) {
	now := time.Now()
	var previous certificateFailure
	for _, minutes := range []int{1, 2, 4, 8, 16, 32, 32} {
		previous = nextCertificateFailure(context.DeadlineExceeded, previous, now)
		if previous.RetryAt.Sub(now) != time.Duration(minutes)*time.Minute {
			t.Fatal("transient retries do not back off")
		}
	}
	invalid := nextCertificateFailure(&acme.OrderError{Status: "invalid", OrderURL: "https://private-order"}, certificateFailure{}, now)
	if invalid.RetryAt.Before(now.Add(15 * time.Minute)) {
		t.Fatal("validation failures repeat too quickly")
	}
	limited := nextCertificateFailure(&acme.Error{ProblemType: "urn:ietf:params:acme:error:rateLimited"}, certificateFailure{}, now)
	if limited.RetryAt.Before(now.Add(time.Hour)) {
		t.Fatal("missing retry header causes immediate retry")
	}
}

func TestUpgradeRetainsKnownCertificateLimit(t *testing.T) {
	dir := t.TempDir()
	now := time.Now().UTC()
	old := status{Phase: "stopped", Diagnostics: &diagnosticSnapshot{Recent: []diagnosticEvent{{Code: "certificate_acme_rateLimited", At: now}}}}
	if err := writeJSON(filepath.Join(dir, "status.json"), old); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(dir, "certificate-retry.json")
	restored, err := readCertificateFailure(path)
	if err != nil || !restored.RetryAt.Equal(now.Add(time.Hour)) {
		t.Fatal("upgrade lost known limit")
	}
	// A subsequent launch must use the persisted deadline, even when status changes.
	writeJSON(filepath.Join(dir, "status.json"), status{Phase: "starting"})
	next, err := readCertificateFailure(path)
	if err != nil || !next.RetryAt.Equal(restored.RetryAt) {
		t.Fatal("startup reset the deadline")
	}
}

func TestCertificateErrorInspectionDoesNotTrustArbitraryProviders(t *testing.T) {
	for _, address := range []string{"http://acme-v02.api.letsencrypt.org/acme/order/1", "https://acme-v02.api.letsencrypt.org.evil.test/", "https://user@acme-v02.api.letsencrypt.org/", "https://127.0.0.1/", "https://acme-v02.api.letsencrypt.org:444/"} {
		if certificateProviderURL(address) {
			t.Fatal("untrusted provider URL accepted")
		}
		err := &acme.OrderError{OrderURL: address, Status: "invalid"}
		if explainCertificateFailure(context.Background(), t.TempDir(), err) != err {
			t.Fatal("unsafe failure changed")
		}
	}
	cause := &acme.Error{ProblemType: "urn:ietf:params:acme:error:dns", Detail: "private-domain"}
	err := &acme.OrderError{Problem: cause}
	if explainCertificateFailure(context.Background(), t.TempDir(), err) != cause {
		t.Fatal("lost specific provider error")
	}
	d := &connectionDiagnostics{}
	d.logf("acme: getCertPEM: %v", err)
	if d.takeCertificateError() != err || d.takeCertificateError() != nil {
		t.Fatal("stale error reused across attempts")
	}
	data, _ := json.Marshal(d.snapshot())
	if strings.Contains(string(data), "private-") {
		t.Fatal("raw error escaped diagnostics")
	}
}

func TestInvalidCertificateRetryStateDoesNotResetSilently(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "certificate-retry.json")
	os.WriteFile(path, []byte("{}"), 0600)
	if _, err := readCertificateFailure(path); err == nil {
		t.Fatal("invalid state reset cooldown")
	}
	os.Remove(path)
	other := filepath.Join(dir, "other")
	os.WriteFile(other, []byte("{}"), 0600)
	os.Symlink(other, path)
	if _, err := readCertificateFailure(path); err == nil {
		t.Fatal("followed retry state symlink")
	}
	os.Remove(path)
	missing, err := readCertificateFailure(path)
	if err != nil || !missing.RetryAt.IsZero() {
		t.Fatal("fresh installation blocked")
	}
	if !errors.Is(explainCertificateFailure(context.Background(), dir, context.DeadlineExceeded), context.DeadlineExceeded) {
		t.Fatal("network error lost")
	}
}
