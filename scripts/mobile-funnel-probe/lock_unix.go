//go:build darwin || linux

package main

import (
	"errors"
	"os"
	"path/filepath"

	"golang.org/x/sys/unix"
)

// OS ownership is released even after a crash; identity and credentials remain.
func lockState(directory string) (func(), error) {
	f, err := os.OpenFile(filepath.Join(directory, "probe.lock"), os.O_CREATE|os.O_RDWR|unix.O_NOFOLLOW, 0600)
	if err != nil {
		return nil, err
	}
	if err := unix.Flock(int(f.Fd()), unix.LOCK_EX|unix.LOCK_NB); err != nil {
		f.Close()
		return nil, errors.New("this probe state is already in use")
	}
	return func() { unix.Flock(int(f.Fd()), unix.LOCK_UN); f.Close() }, nil
}
