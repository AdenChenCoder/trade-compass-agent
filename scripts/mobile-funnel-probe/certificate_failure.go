package main

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"encoding/pem"
	"errors"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"time"

	"tailscale.com/client/local"
	"tailscale.com/tempfork/acme"
)

// Only fixed categories and deadlines are persisted. The original error remains
// in memory so a failed order can be inspected without submitting another one.
type certificateFailure struct {
	Code     string    `json:"code"`
	RetryAt  time.Time `json:"retry_at"`
	Attempts int       `json:"attempts"`
}

func nextCertificateFailure(err error, previous certificateFailure, now time.Time) certificateFailure {
	attempts := min(previous.Attempts+1, 1000000)
	delay := time.Minute * time.Duration(1<<min(attempts-1, 5))
	code := certificateFailureCode(err)
	if code == "certificate_validation_failed" || strings.HasPrefix(code, "certificate_acme_") {
		delay = max(delay, 15*time.Minute)
	}
	var problem *acme.Error
	if errors.As(err, &problem) {
		if retry, limited := acme.RateLimit(problem); limited {
			// A missing Retry-After must not become an immediate new issuance request.
			if retry <= 0 {
				retry = time.Hour
			}
			delay = max(delay, retry)
		}
	}
	if retry, limited := local.RateLimitRetryAfter(err); limited {
		code = "certificate_acme_rateLimited"
		delay = max(delay, retry, time.Minute)
	}
	return certificateFailure{Code: code, RetryAt: now.Add(delay), Attempts: attempts}
}

func readCertificateFailure(path string) (certificateFailure, error) {
	var result certificateFailure
	info, err := os.Lstat(path)
	if errors.Is(err, os.ErrNotExist) {
		// Upgrade from helpers that recorded a limit but did not persist its
		// deadline. Do not let an application upgrade bypass a known limit.
		oldPath := filepath.Join(filepath.Dir(path), "status.json")
		if info, err := os.Lstat(oldPath); err == nil && info.Mode().IsRegular() && info.Mode().Perm()&0077 == 0 && info.Size() <= 8192 {
			data, _ := os.ReadFile(oldPath)
			var old status
			if json.Unmarshal(data, &old) == nil && old.Diagnostics != nil {
				for _, event := range old.Diagnostics.Recent {
					if event.Code == "certificate_acme_rateLimited" {
						deadline := event.At.Add(time.Hour)
						if deadline.After(time.Now()) && deadline.After(result.RetryAt) {
							result = certificateFailure{Code: event.Code, RetryAt: deadline, Attempts: 1}
						}
					}
				}
			}
		}
		if !result.RetryAt.IsZero() {
			return result, writeJSON(path, result)
		}
		return result, nil
	}
	if err != nil {
		return result, err
	}
	if !info.Mode().IsRegular() || info.Mode().Perm()&0077 != 0 || info.Size() > 1024 {
		return result, errors.New("invalid certificate retry state")
	}
	data, err := os.ReadFile(path)
	if err != nil {
		return result, err
	}
	if err := json.Unmarshal(data, &result); err != nil {
		return certificateFailure{}, err
	}
	if !strings.HasPrefix(result.Code, "certificate_") || result.Attempts < 0 || result.Attempts > 1000000 || result.RetryAt.IsZero() {
		return certificateFailure{}, errors.New("invalid certificate retry state")
	}
	return result, nil
}

// Do not follow arbitrary URLs from provider errors with the account credential.
func certificateProviderURL(raw string) bool {
	u, err := url.Parse(raw)
	return err == nil && u.Scheme == "https" && u.Host == "acme-v02.api.letsencrypt.org" && u.User == nil && u.Fragment == ""
}

type certificateReadTransport struct{}

func (certificateReadTransport) RoundTrip(r *http.Request) (*http.Response, error) {
	if !certificateProviderURL(r.URL.String()) {
		return nil, errors.New("unexpected certificate provider URL")
	}
	return http.DefaultTransport.RoundTrip(r)
}

// Read the challenge behind an invalid order: WaitOrder often omits its cause.
// This performs no registration, new order, challenge acceptance or finalization.
func explainCertificateFailure(ctx context.Context, directory string, err error) error {
	var orderError *acme.OrderError
	if !errors.As(err, &orderError) {
		return err
	}
	if orderError.Problem != nil {
		return orderError.Problem
	}
	if !certificateProviderURL(orderError.OrderURL) {
		return err
	}
	data, readErr := os.ReadFile(filepath.Join(directory, "tsnet", "certs", "acme-account.key.pem"))
	if readErr != nil {
		return err
	}
	block, _ := pem.Decode(data)
	if block == nil {
		return err
	}
	key, parseErr := x509.ParseECPrivateKey(block.Bytes)
	if parseErr != nil {
		return err
	}
	ctx, cancel := context.WithTimeout(ctx, 15*time.Second)
	defer cancel()
	client := &acme.Client{Key: key, HTTPClient: &http.Client{
		Transport: certificateReadTransport{}, Timeout: 10 * time.Second,
		CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
	}}
	order, fetchErr := client.GetOrder(ctx, orderError.OrderURL)
	if fetchErr != nil || len(order.AuthzURLs) > 2 {
		return err
	}
	for _, authURL := range order.AuthzURLs {
		if !certificateProviderURL(authURL) {
			continue
		}
		auth, fetchErr := client.GetAuthorization(ctx, authURL)
		if fetchErr != nil {
			continue
		}
		for _, challenge := range auth.Challenges {
			if challenge.Type == "dns-01" && challenge.Error != nil {
				return challenge.Error
			}
		}
	}
	return err
}

// A valid cached certificate remains usable while a renewal is backing off.
func cachedCertificateReady(directory, host string, now time.Time) bool {
	prefix := filepath.Join(directory, "tsnet", "certs", host)
	pair, err := tls.LoadX509KeyPair(prefix+".crt", prefix+".key")
	if err != nil || len(pair.Certificate) == 0 {
		return false
	}
	leaf, err := x509.ParseCertificate(pair.Certificate[0])
	if err != nil {
		return false
	}
	roots := x509.NewCertPool()
	for _, data := range pair.Certificate[1:] {
		cert, err := x509.ParseCertificate(data)
		if err != nil {
			return false
		}
		roots.AddCert(cert)
	}
	_, err = leaf.Verify(x509.VerifyOptions{DNSName: host, CurrentTime: now, Intermediates: roots})
	return err == nil
}
