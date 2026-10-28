package main

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"sync/atomic"
	"testing"
	"time"
)

func TestSlowCertificateKeepsPreparationStatusFresh(t *testing.T) {
	path := filepath.Join(t.TempDir(), "status.json")
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	var pulses atomic.Int32
	fetch := func(ctx context.Context) ([]byte, []byte, error) {
		for pulses.Load() < 3 {
			select {
			case <-ctx.Done():
				return nil, nil, ctx.Err()
			case <-time.After(time.Millisecond):
			}
		}
		data, err := os.ReadFile(path)
		if err != nil {
			return nil, nil, err
		}
		var value status
		if err := json.Unmarshal(data, &value); err != nil {
			return nil, nil, err
		}
		if value.Phase != "preparing_certificate" || value.ActionURL != "" || time.Since(value.UpdatedAt) > time.Second {
			return nil, nil, errors.New("parent would see stale or misleading certificate state")
		}
		return []byte("test-cert"), []byte("test-key"), nil
	}
	cert, key, err := prepareCertificate(ctx, fetch, func() error {
		err := writeJSON(path, status{Phase: "preparing_certificate", UpdatedAt: time.Now().UTC()})
		if err == nil {
			pulses.Add(1)
		}
		return err
	}, 5*time.Millisecond)
	if err != nil || string(cert) != "test-cert" || string(key) != "test-key" {
		t.Fatalf("slow issuance failed: %v", err)
	}
}

func TestCertificateWaitStopsWithParentAndPreservesFailures(t *testing.T) {
	for _, source := range []string{"parent", "heartbeat", "provider"} {
		t.Run(source, func(t *testing.T) {
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			problem := errors.New("test failure")
			stopped := make(chan struct{})
			fetch := func(ctx context.Context) ([]byte, []byte, error) {
				defer close(stopped)
				if source == "provider" {
					return nil, nil, problem
				}
				<-ctx.Done()
				return nil, nil, ctx.Err()
			}
			cert, key, err := prepareCertificate(ctx, fetch, func() error {
				if source == "parent" {
					cancel()
					return nil
				}
				return problem
			}, time.Millisecond)
			want := problem
			if source == "parent" {
				want = context.Canceled
			}
			if !errors.Is(err, want) || cert != nil || key != nil {
				t.Fatalf("failure was lost: %v", err)
			}
			select {
			case <-stopped:
			case <-time.After(time.Second):
				t.Fatal("certificate request survived cancellation")
			}
		})
	}
}
