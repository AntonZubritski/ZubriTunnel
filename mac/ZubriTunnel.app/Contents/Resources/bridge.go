//go:build darwin

// Bridge mode: a local HTTP CONNECT + plain HTTP proxy whose outbound
// sockets are forcibly routed through a specific network interface (a utun
// device created by OpenVPN) via macOS's IP_BOUND_IF setsockopt.
//
// Usage:
//   vpn-proxy -bridge-iface utun6 -addr 127.0.0.1:8081
//
// Apps that set HTTPS_PROXY=http://127.0.0.1:8081 will have their TCP traffic
// leave through utun6 (= through the OpenVPN tunnel). Apps that don't set the
// proxy continue to use the default route → unaffected by VPN. This achieves
// per-app OpenVPN routing on macOS without kernel hacks.

package main

import (
	"bufio"
	"context"
	"errors"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"net/url"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"golang.org/x/sys/unix"
)

// runBridgeMode listens on `addr` and forwards TCP traffic through `iface`.
// Returns when the process receives SIGINT/SIGTERM.
func runBridgeMode(iface, addr string) error {
	netIface, err := net.InterfaceByName(iface)
	if err != nil {
		return fmt.Errorf("interface %q not found: %w", iface, err)
	}
	log.Printf("bridge: outbound bound to %s (index=%d)", iface, netIface.Index)

	// Custom dialer that pins each outbound socket to the chosen interface.
	dialer := &net.Dialer{
		Timeout:   20 * time.Second,
		KeepAlive: 30 * time.Second,
		Control: func(network, address string, c syscall.RawConn) error {
			var setErr error
			err := c.Control(func(fd uintptr) {
				// IP_BOUND_IF for IPv4, IPV6_BOUND_IF for IPv6 — both are
				// macOS extensions. We set the matching one based on the
				// network family. setsockopt with the wrong level is a
				// no-op-error on Darwin, so we try both defensively.
				if strings.HasSuffix(network, "6") {
					setErr = unix.SetsockoptInt(int(fd),
						unix.IPPROTO_IPV6, unix.IPV6_BOUND_IF, netIface.Index)
				} else {
					setErr = unix.SetsockoptInt(int(fd),
						unix.IPPROTO_IP, unix.IP_BOUND_IF, netIface.Index)
				}
			})
			if err != nil {
				return err
			}
			return setErr
		},
	}

	// HTTP server that handles both CONNECT (for HTTPS) and plain proxy
	// requests (for HTTP). The transport reuses our pinned dialer.
	transport := &http.Transport{
		DialContext:           dialer.DialContext,
		ForceAttemptHTTP2:     false, // many origin servers misbehave when h2 from proxy
		IdleConnTimeout:       90 * time.Second,
		TLSHandshakeTimeout:   15 * time.Second,
		ExpectContinueTimeout: 1 * time.Second,
		MaxIdleConns:          100,
	}

	srv := &http.Server{
		Addr: addr,
		Handler: http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if r.Method == http.MethodConnect {
				handleConnect(w, r, dialer)
				return
			}
			handlePlainProxy(w, r, transport)
		}),
		// Disable HTTP/2 on the listener side too (browsers send h1.1 to proxies)
		ReadHeaderTimeout: 30 * time.Second,
	}

	// Graceful shutdown on SIGINT/SIGTERM — guarantees the listener is closed
	// and outstanding connections drain. Without this the process can leave
	// a zombie listener if the parent (ZubriTunnel GUI) crashes.
	shutdownCh := make(chan os.Signal, 1)
	signal.Notify(shutdownCh, os.Interrupt, syscall.SIGTERM)
	go func() {
		<-shutdownCh
		log.Println("bridge: shutdown signal received")
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		_ = srv.Shutdown(ctx)
	}()

	log.Printf("bridge: listening on %s — set HTTPS_PROXY=http://%s in apps you want VPN'd", addr, addr)
	if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		return err
	}
	return nil
}

// handleConnect implements the HTTP CONNECT method (used for HTTPS tunneling).
// Client sends "CONNECT host:port HTTP/1.1", we open a TCP connection through
// our pinned dialer, then bidirectionally pipe bytes between client and server.
func handleConnect(w http.ResponseWriter, r *http.Request, dialer *net.Dialer) {
	target := r.Host
	if !strings.Contains(target, ":") {
		target = target + ":443"
	}
	upstream, err := dialer.DialContext(r.Context(), "tcp", target)
	if err != nil {
		http.Error(w, err.Error(), http.StatusBadGateway)
		log.Printf("bridge: CONNECT %s dial failed: %v", target, err)
		return
	}
	defer upstream.Close()

	// We need raw access to the client's TCP socket to pipe bytes. http.Hijacker
	// is the standard way (works for HTTP/1.1, which is what proxies use).
	hj, ok := w.(http.Hijacker)
	if !ok {
		http.Error(w, "hijacking unsupported", http.StatusInternalServerError)
		return
	}
	clientConn, clientBuf, err := hj.Hijack()
	if err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}
	defer clientConn.Close()

	// Tell the client the tunnel is open
	_, _ = clientConn.Write([]byte("HTTP/1.1 200 Connection Established\r\n\r\n"))

	// Pipe both directions in parallel until either side closes
	done := make(chan struct{}, 2)
	go func() {
		_, _ = io.Copy(upstream, clientBuf) // client → upstream
		// half-close the upstream write side so the server sees EOF
		if tcp, ok := upstream.(*net.TCPConn); ok {
			_ = tcp.CloseWrite()
		}
		done <- struct{}{}
	}()
	go func() {
		_, _ = io.Copy(clientConn, upstream) // upstream → client
		if tcp, ok := clientConn.(*net.TCPConn); ok {
			_ = tcp.CloseWrite()
		}
		done <- struct{}{}
	}()
	<-done
	<-done
}

// handlePlainProxy serves plain (non-CONNECT) HTTP proxy requests — i.e. the
// client sends "GET http://example.com/foo HTTP/1.1" with absolute URL, we
// fetch it via our pinned transport and stream the response back.
func handlePlainProxy(w http.ResponseWriter, r *http.Request, transport *http.Transport) {
	// Reject obvious bad requests fast
	if !r.URL.IsAbs() {
		http.Error(w, "this is a proxy — request URL must be absolute", http.StatusBadRequest)
		return
	}

	// Build outbound request, dropping hop-by-hop headers that must not be forwarded
	out := r.Clone(r.Context())
	out.RequestURI = ""
	stripHopHeaders(out.Header)

	// Send via our pinned transport
	resp, err := transport.RoundTrip(out)
	if err != nil {
		http.Error(w, err.Error(), http.StatusBadGateway)
		log.Printf("bridge: %s %s failed: %v", r.Method, r.URL, err)
		return
	}
	defer resp.Body.Close()

	stripHopHeaders(resp.Header)
	for k, vs := range resp.Header {
		for _, v := range vs {
			w.Header().Add(k, v)
		}
	}
	w.WriteHeader(resp.StatusCode)
	_, _ = io.Copy(w, resp.Body)
}

func stripHopHeaders(h http.Header) {
	// Per RFC 7230 §6.1, these headers are hop-by-hop and must not be forwarded
	for _, name := range []string{
		"Connection", "Proxy-Connection", "Proxy-Authenticate",
		"Proxy-Authorization", "Te", "Trailer", "Transfer-Encoding", "Upgrade",
		"Keep-Alive",
	} {
		h.Del(name)
	}
}

// Suppress unused-import warnings if the rest of main.go doesn't reference these
var (
	_ = bufio.NewReader
	_ = url.Parse
)
