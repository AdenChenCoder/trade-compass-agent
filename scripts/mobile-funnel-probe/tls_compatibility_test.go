package main

import (
	"bytes"
	"crypto/tls"
	"encoding/binary"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

// Some deployed TLS inspectors reject an unfamiliar first certificate signature
// scheme rather than ignoring it. Go 1.27.4 addresses this incompatibility;
// see tailscale/tailscale#21708 and golang/go#81199. Exercise a real, verified
// HTTPS exchange through that boundary, without a node, proxy or public service.
func TestControlTLSCertificateSignatureCompatibility(t *testing.T) {
	events := make(chan helloCompatibility, 2)
	server := httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.TLS == nil || r.TLS.Version < tls.VersionTLS12 {
			t.Error("request did not retain TLS protection")
		}
		_, _ = io.WriteString(w, "control transport available")
	}))
	server.Listener = &certificateInspector{Listener: server.Listener, events: events}
	server.Config.ReadHeaderTimeout = 3 * time.Second
	server.StartTLS()
	defer server.Close()
	client := server.Client()
	client.Timeout = 5 * time.Second
	client.Transport.(*http.Transport).DisableKeepAlives = true
	for _, phase := range []string{"cold", "reconnect"} {
		t.Run(phase, func(t *testing.T) {
			response, err := client.Get(server.URL)
			var hello helloCompatibility
			select {
			case hello = <-events:
			case <-time.After(time.Second):
				t.Fatal("TLS client did not reach the inspector")
			}
			t.Logf("certificate schemes=%x; supported groups=%x", hello.certificateSchemes, hello.groups)
			if err != nil {
				t.Fatalf("verified HTTPS failed through the certificate-signature inspector: %v", err)
			}
			defer response.Body.Close()
			body, err := io.ReadAll(response.Body)
			if err != nil || response.StatusCode != http.StatusOK || string(body) != "control transport available" {
				t.Fatalf("HTTPS response incomplete: status=%d error=%v", response.StatusCode, err)
			}
			if response.TLS == nil || len(response.TLS.VerifiedChains) == 0 {
				t.Fatal("certificate verification was bypassed")
			}
			// Retain the default hybrid key exchange; compatibility must not
			// depend on disabling post-quantum TLS with environment flags.
			hybrid := false
			for i := 0; i+1 < len(hello.groups); i += 2 {
				hybrid = hybrid || tls.CurveID(binary.BigEndian.Uint16(hello.groups[i:])) == tls.X25519MLKEM768
			}
			if !hybrid {
				t.Fatal("X25519MLKEM768 was removed from the supported groups")
			}
		})
	}
}

type helloCompatibility struct {
	certificateSchemes []byte
	groups             []byte
}

type certificateInspector struct {
	net.Listener
	events chan<- helloCompatibility
}

func (l *certificateInspector) Accept() (net.Conn, error) {
	for {
		connection, err := l.Listener.Accept()
		if err != nil {
			return nil, err
		}
		_ = connection.SetReadDeadline(time.Now().Add(5 * time.Second))
		records, hello, err := readCompatibilityHello(connection)
		if err != nil {
			_ = connection.Close()
			return nil, err
		}
		l.events <- hello
		if len(hello.certificateSchemes) >= 2 && !legacyCertificateScheme(binary.BigEndian.Uint16(hello.certificateSchemes)) {
			_ = connection.Close()
			continue
		}
		_ = connection.SetReadDeadline(time.Time{})
		return &replayedHelloConn{Conn: connection, reader: io.MultiReader(bytes.NewReader(records), connection)}, nil
	}
}

func legacyCertificateScheme(scheme uint16) bool {
	switch tls.SignatureScheme(scheme) {
	case tls.PSSWithSHA256, tls.PSSWithSHA384, tls.PSSWithSHA512,
		tls.ECDSAWithP256AndSHA256, tls.ECDSAWithP384AndSHA384, tls.ECDSAWithP521AndSHA512,
		tls.PKCS1WithSHA256, tls.PKCS1WithSHA384, tls.PKCS1WithSHA512, tls.Ed25519:
		return true
	}
	return false
}

type replayedHelloConn struct {
	net.Conn
	reader io.Reader
}

func (c *replayedHelloConn) Read(p []byte) (int, error) { return c.reader.Read(p) }

func readCompatibilityHello(reader io.Reader) ([]byte, helloCompatibility, error) {
	var records, handshake []byte
	var hello helloCompatibility
	for len(handshake) < 4 || len(handshake) < 4+(int(handshake[1])<<16|int(handshake[2])<<8|int(handshake[3])) {
		var header [5]byte
		if _, err := io.ReadFull(reader, header[:]); err != nil {
			return nil, hello, err
		}
		length := int(binary.BigEndian.Uint16(header[3:]))
		if header[0] != 22 || length == 0 || len(handshake)+length > 65536 {
			return nil, hello, fmt.Errorf("invalid ClientHello record")
		}
		payload := make([]byte, length)
		if _, err := io.ReadFull(reader, payload); err != nil {
			return nil, hello, err
		}
		records = append(records, header[:]...)
		records = append(records, payload...)
		handshake = append(handshake, payload...)
	}
	if handshake[0] != 1 || len(handshake) < 39 {
		return nil, hello, fmt.Errorf("invalid ClientHello handshake")
	}
	data := handshake[4+2+32:]
	vector := func(width int) ([]byte, error) {
		if len(data) < width {
			return nil, io.ErrUnexpectedEOF
		}
		length := int(data[0])
		if width == 2 {
			length = int(binary.BigEndian.Uint16(data))
		}
		if len(data) < width+length {
			return nil, io.ErrUnexpectedEOF
		}
		value := data[width : width+length]
		data = data[width+length:]
		return value, nil
	}
	for _, width := range []int{1, 2, 1} {
		if _, err := vector(width); err != nil {
			return nil, hello, err
		}
	}
	extensions, err := vector(2)
	if err != nil {
		return nil, hello, err
	}
	for len(extensions) >= 4 {
		kind, length := binary.BigEndian.Uint16(extensions), int(binary.BigEndian.Uint16(extensions[2:]))
		if len(extensions) < 4+length {
			return nil, hello, io.ErrUnexpectedEOF
		}
		value := extensions[4 : 4+length]
		if (kind == 10 || kind == 50) && len(value) >= 2 && int(binary.BigEndian.Uint16(value)) == len(value)-2 {
			if kind == 10 {
				hello.groups = bytes.Clone(value[2:])
			} else {
				hello.certificateSchemes = bytes.Clone(value[2:])
			}
		}
		extensions = extensions[4+length:]
	}
	return records, hello, nil
}
