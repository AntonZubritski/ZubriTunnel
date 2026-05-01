//go:build !darwin

// Bridge mode is macOS-only — it relies on IP_BOUND_IF setsockopt which is
// a Darwin extension. On Windows / Linux the per-app OpenVPN bridge needs a
// different mechanism (Linux: SO_BINDTODEVICE; Windows: bind by source IP).
// For now bail out clearly so users know the feature isn't supported here.

package main

import (
	"fmt"
	"runtime"
)

func runBridgeMode(iface, addr string) error {
	return fmt.Errorf("bridge mode is implemented for macOS only (got %s)", runtime.GOOS)
}
