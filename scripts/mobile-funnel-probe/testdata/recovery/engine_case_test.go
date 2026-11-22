package wgengine

import (
	"bytes"
	"crypto/ecdh"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"github.com/tailscale/wireguard-go/conn/bindtest"
	"github.com/tailscale/wireguard-go/device"
	"github.com/tailscale/wireguard-go/tun/tuntest"
	"go4.org/mem"
	"net/netip"
	"strconv"
	"strings"
	"tailscale.com/types/key"
	"tailscale.com/types/logger"
	"tailscale.com/wgengine/wglog"
	"testing"
	"time"
)

func TestDiscoUpdatePreservesExistingInboundSession(t *testing.T) {
	for _, mode := range []string{"actual_sdk_operation"} {
		t.Run(mode, func(t *testing.T) {
			binds := bindtest.NewChannelBinds()
			var devs [2]*device.Device
			var tunnels [2]*tuntest.ChannelTUN
			var keys [2]*ecdh.PrivateKey
			var ports [2]string
			for i := range 2 {
				var err error
				keys[i], err = ecdh.X25519().GenerateKey(rand.Reader)
				if err != nil {
					t.Fatal(err)
				}
				tunnels[i] = tuntest.NewChannelTUN()
				devs[i] = device.NewDevice(tunnels[i].TUN(), binds[i], device.NewLogger(device.LogLevelSilent, ""))
				t.Cleanup(devs[i].Close)
				if err := devs[i].IpcSet("private_key=" + hex.EncodeToString(keys[i].Bytes()) + "\nlisten_port=0\n"); err != nil {
					t.Fatal(err)
				}
				if err := devs[i].Up(); err != nil {
					t.Fatal(err)
				}
				config, err := devs[i].IpcGet()
				if err != nil {
					t.Fatal(err)
				}
				for _, line := range strings.Split(config, "\n") {
					if strings.HasPrefix(line, "listen_port=") {
						ports[i] = strings.TrimPrefix(line, "listen_port=")
					}
				}
				if _, err := strconv.Atoi(ports[i]); err != nil {
					t.Fatal("missing in-memory transport port")
				}
			}
			peerConfig := func(i int) string {
				return fmt.Sprintf("public_key=%s\nendpoint=127.0.0.1:%s\nallowed_ip=1.0.0.%d/32\n", hex.EncodeToString(keys[1-i].PublicKey().Bytes()), ports[1-i], 2-i)
			}
			for i := range 2 {
				if err := devs[i].IpcSet(peerConfig(i)); err != nil {
					t.Fatal(err)
				}
			}
			send := func(from int, timeout time.Duration) bool {
				pkt := tuntest.Ping(netip.MustParseAddr(fmt.Sprintf("1.0.0.%d", 2-from)), netip.MustParseAddr(fmt.Sprintf("1.0.0.%d", from+1)))
				select {
				case tunnels[from].Outbound <- pkt:
				case <-time.After(timeout):
					t.Fatal("outbound timed out")
				}
				select {
				case got := <-tunnels[1-from].Inbound:
					return bytes.Equal(got, pkt)
				case <-time.After(timeout):
					return false
				}
			}
			if !send(0, 3*time.Second) || !send(1, 3*time.Second) {
				t.Fatal("initial bidirectional encrypted session failed")
			}
			peer := key.NodePublicFromRaw32(mem.B(keys[0].PublicKey().Bytes()))
			engine := &userspaceEngine{wgdev: devs[1], wgLogger: wglog.NewLogger(logger.Discard, nil)}
			engine.RECOVERY_OPERATION(peer)
			delivered := send(0, 500*time.Millisecond)
			evidence, _ := json.Marshal(map[string]any{"recovery_scene": "session_continuity", "baseline_bidirectional": true, "incoming_packet_delivered": delivered})
			t.Log(string(evidence))
			if !delivered {
				t.Fatal("discovery update destroyed a valid established inbound session")
			}
			t.Logf("incoming application packet delivered=%v", delivered)
		})
	}
}
