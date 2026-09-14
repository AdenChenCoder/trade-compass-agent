//go:build !darwin && !linux

package main

import "errors"

func lockState(string) (func(), error) {
	return nil, errors.New("this diagnostic currently supports macOS and Linux only")
}
