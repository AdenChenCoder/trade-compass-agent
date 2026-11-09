package main

import (
	"context"
	"net"
	"net/http"
	"net/netip"
	"os"
	"runtime"
	"testing"
	"time"

	"tailscale.com/net/netmon"
	"tailscale.com/net/netns"
	"tailscale.com/net/tshttpproxy"
	"tailscale.com/types/logger"
)

// Opt in only on a Mac whose existing TUN proxy resolves the control host to
// 198.18.0.0/15. No node, login, certificate issuance or business state is used.
func TestMacOSProxyRouteHTTPS(t *testing.T) {
	if runtime.GOOS != "darwin" || os.Getenv("COMPASS_TEST_MACOS_PROXY_ROUTE") != "1" {
		t.Skip("requires explicit macOS TUN proxy integration check")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 25*time.Second)
	defer cancel()
	addresses, err := net.DefaultResolver.LookupNetIP(ctx, "ip4", "controlplane.tailscale.com")
	if err != nil || len(addresses) == 0 {
		t.Fatal("control hostname did not resolve")
	}
	for _, address := range addresses {
		if !netip.MustParsePrefix("198.18.0.0/15").Contains(address) {
			t.Fatal("integration check requires the existing proxy's synthetic DNS route")
		}
	}
	for _, phase := range []string{"startup", "after_network_map_update"} {
		t.Run(phase, func(t *testing.T) {
			// Recreate the transport after the SDK's normal route capability
			// reset, keeping its physical-interface and routing-loop protection.
			netns.SetBindToInterfaceByRoute(logger.Discard, false)
			dialer := netns.NewDialer(t.Logf, netmon.NewStatic())
			transport := &http.Transport{DialContext: dialer.DialContext, DisableKeepAlives: true,
				Proxy: tshttpproxy.ProxyFromEnvironment}
			defer transport.CloseIdleConnections()
			client := &http.Client{Transport: transport, Timeout: 10 * time.Second,
				CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
			request, err := http.NewRequestWithContext(ctx, http.MethodHead, "https://controlplane.tailscale.com/", nil)
			if err != nil {
				t.Fatal(err)
			}
			response, err := client.Do(request)
			if err != nil {
				t.Fatal(err)
			}
			defer response.Body.Close()
			if response.StatusCode < 200 || response.StatusCode >= 400 || response.TLS == nil || len(response.TLS.VerifiedChains) == 0 {
				t.Fatalf("expected CA-verified HTTPS response, got %d", response.StatusCode)
			}
		})
	}
}
