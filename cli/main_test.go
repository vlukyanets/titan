package main

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestNoCommandPrintsUsage(t *testing.T) {
	var stdout, stderr bytes.Buffer
	if code := run(nil, nil, &stdout, &stderr); code != 2 {
		t.Errorf("exit code = %d, want 2", code)
	}
	if stderr.String() != usage {
		t.Errorf("stderr = %q, want %q", stderr.String(), usage)
	}
}

// fakeNode signs in alice with the code "c0de" and knows the token "t0ken".
func fakeNode(t *testing.T) *httptest.Server {
	t.Helper()
	mux := http.NewServeMux()
	mux.HandleFunc("POST /sign-in", func(w http.ResponseWriter, r *http.Request) {
		var body map[string]string
		json.NewDecoder(r.Body).Decode(&body)
		if body["name"] != "alice" || body["code"] != "c0de" {
			w.WriteHeader(http.StatusUnauthorized)
			return
		}
		json.NewEncoder(w).Encode(map[string]string{"token": "t0ken"})
	})
	mux.HandleFunc("GET /me", func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer t0ken" {
			w.WriteHeader(http.StatusUnauthorized)
			return
		}
		json.NewEncoder(w).Encode(map[string]string{"name": "alice"})
	})
	node := httptest.NewServer(mux)
	t.Cleanup(node.Close)
	return node
}

// configDir points the CLI at an empty config folder of its own.
func configDir(t *testing.T) string {
	t.Helper()
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)
	t.Setenv("HOME", dir)
	path, err := os.UserConfigDir()
	if err != nil {
		t.Fatal(err)
	}
	return filepath.Join(path, "titan")
}

func TestLoginSavesTokenOnlyUserCanRead(t *testing.T) {
	node, dir := fakeNode(t), configDir(t)
	var stdout, stderr bytes.Buffer
	if code := run([]string{"login", node.URL}, strings.NewReader("alice\nc0de\n"), &stdout, &stderr); code != 0 {
		t.Fatalf("exit code = %d, stderr = %q", code, stderr.String())
	}
	info, err := os.Stat(filepath.Join(dir, "token"))
	if err != nil {
		t.Fatal(err)
	}
	if perm := info.Mode().Perm(); perm != 0o600 {
		t.Errorf("token file mode = %o, want 600", perm)
	}
	data, _ := os.ReadFile(filepath.Join(dir, "token"))
	if !strings.Contains(string(data), "t0ken") {
		t.Errorf("token file = %q, want the token", data)
	}
	if strings.Contains(stdout.String()+stderr.String(), "t0ken") {
		t.Error("login printed the token")
	}
}

func TestWhoamiUsesSavedTokenWithoutAsking(t *testing.T) {
	node, _ := fakeNode(t), configDir(t)
	var stdout, stderr bytes.Buffer
	run([]string{"login", node.URL}, strings.NewReader("alice\nc0de\n"), &stdout, &stderr)
	stdout.Reset()
	stderr.Reset()
	if code := run([]string{"whoami"}, strings.NewReader(""), &stdout, &stderr); code != 0 {
		t.Fatalf("exit code = %d, stderr = %q", code, stderr.String())
	}
	if stdout.String() != "alice\n" {
		t.Errorf("stdout = %q, want %q", stdout.String(), "alice\n")
	}
	if stderr.String() != "" {
		t.Errorf("stderr = %q, want nothing", stderr.String())
	}
}

func TestRefusedLoginSavesNothing(t *testing.T) {
	node, dir := fakeNode(t), configDir(t)
	var stdout, stderr bytes.Buffer
	if code := run([]string{"login", node.URL}, strings.NewReader("alice\nwrong\n"), &stdout, &stderr); code != 1 {
		t.Errorf("exit code = %d, want 1", code)
	}
	if _, err := os.Stat(filepath.Join(dir, "token")); err == nil {
		t.Error("a refused login saved a token file")
	}
}
