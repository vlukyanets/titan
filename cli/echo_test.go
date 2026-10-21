//go:build linux

package main

import (
	"bytes"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/creack/pty"
	"golang.org/x/sys/unix"
)

// TestKeyIsTypedWithoutEcho types into a real terminal: the URL and the
// model name show as typed, the key does not.
func TestKeyIsTypedWithoutEcho(t *testing.T) {
	titan := signedIn(t)
	terminal, tty, err := pty.Open()
	if err != nil {
		t.Fatal(err)
	}
	defer terminal.Close()
	defer tty.Close()

	var mu sync.Mutex
	var screen bytes.Buffer
	go func() {
		buf := make([]byte, 1024)
		for {
			n, err := terminal.Read(buf)
			mu.Lock()
			screen.Write(buf[:n])
			mu.Unlock()
			if err != nil {
				return
			}
		}
	}()
	shown := func() string {
		mu.Lock()
		defer mu.Unlock()
		return screen.String()
	}
	waitFor := func(what string, done func() bool) {
		t.Helper()
		for deadline := time.Now().Add(5 * time.Second); !done(); time.Sleep(10 * time.Millisecond) {
			if time.Now().After(deadline) {
				t.Fatalf("no %s; the terminal shows %q", what, shown())
			}
		}
	}
	prompt := func(p string) {
		t.Helper()
		waitFor("prompt "+p, func() bool { return strings.Contains(shown(), p) })
	}

	code := make(chan int, 1)
	go func() { code <- run([]string{"settings", "model", "add"}, tty, tty, tty) }()
	prompt("URL: ")
	terminal.WriteString("https://models.example/v1\n")
	prompt("Model: ")
	terminal.WriteString("big\n")
	prompt("Key (empty for none): ")
	// The prompt comes just before echo goes off; type only after it has.
	waitFor("echo off", func() bool {
		termios, err := unix.IoctlGetTermios(int(tty.Fd()), unix.TCGETS)
		return err == nil && termios.Lflag&unix.ECHO == 0
	})
	terminal.WriteString("sk-secret\n")
	if c := <-code; c != 0 {
		t.Fatalf("exit code = %d; the terminal shows %q", c, shown())
	}
	prompt("Saved.")

	if !strings.Contains(shown(), "big") {
		t.Errorf("the model name was not echoed: %q", shown())
	}
	if strings.Contains(shown(), "sk-secret") {
		t.Errorf("the key was echoed: %q", shown())
	}
	if _, stdout, _ := titan("", "settings"); !strings.Contains(stdout, "key set") {
		t.Errorf("the node did not get the key: %q", stdout)
	}
}
