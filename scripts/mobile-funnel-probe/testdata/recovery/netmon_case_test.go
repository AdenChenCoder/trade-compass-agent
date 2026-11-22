package netmon

import (
	"encoding/json"
	"tailscale.com/types/logger"
	"tailscale.com/util/eventbus"
	"testing"
)

func TestNetworkComesUpAtSubscriptionBoundary(t *testing.T) {
	reproLinkUp = false
	monitor, err := New(eventbus.New(), logger.Discard)
	if err != nil {
		t.Fatal(err)
	}
	defer monitor.Close()
	cached := monitor.InterfaceState().AnyInterfaceUp()
	evidence, _ := json.Marshal(map[string]any{"recovery_scene": "network_availability", "actual_link_up": reproLinkUp, "cached_link_up": cached})
	t.Log(string(evidence))
	if !reproLinkUp || !cached {
		t.Fatal("network became available during construction but consumer sees offline state")
	}
}
