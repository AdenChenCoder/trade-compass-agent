package main

import (
	"context"
	"time"
)

// First issuance can outlast the parent's status freshness window. Keep the
// preparation record live without exposing a listener before TLS is ready.
func prepareCertificate(ctx context.Context, fetch func(context.Context) ([]byte, []byte, error), heartbeat func() error, interval time.Duration) ([]byte, []byte, error) {
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	type result struct {
		cert, key []byte
		err       error
	}
	done := make(chan result, 1)
	go func() {
		cert, key, err := fetch(ctx)
		done <- result{cert, key, err}
	}()
	ticker := time.NewTicker(interval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return nil, nil, ctx.Err()
		case value := <-done:
			return value.cert, value.key, value.err
		case <-ticker.C:
			if err := heartbeat(); err != nil {
				return nil, nil, err
			}
		}
	}
}
