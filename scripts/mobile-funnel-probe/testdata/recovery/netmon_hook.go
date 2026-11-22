package netmon

import (
	"io"
	"tailscale.com/types/logger"
	"tailscale.com/util/eventbus"
)

var reproLinkUp bool

type reproOSMon struct{}

func (*reproOSMon) Close() error              { return nil }
func (*reproOSMon) Receive() (message, error) { return nil, io.EOF }
func newOSMon(_ *eventbus.Bus, _ logger.Logf, _ *Monitor) (osMon, error) {
	reproLinkUp = true
	return &reproOSMon{}, nil
}
