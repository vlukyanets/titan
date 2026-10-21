package main

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"sort"
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
// It keeps her models as the node would: the first one added is current.
func fakeNode(t *testing.T) *httptest.Server {
	t.Helper()
	models := map[string]model{}
	current := ""
	authorized := func(w http.ResponseWriter, r *http.Request) bool {
		if r.Header.Get("Authorization") != "Bearer t0ken" {
			w.WriteHeader(http.StatusUnauthorized)
			return false
		}
		return true
	}
	mux := http.NewServeMux()
	mux.HandleFunc("PUT /settings/models/{name...}", func(w http.ResponseWriter, r *http.Request) {
		if !authorized(w, r) {
			return
		}
		var body map[string]string
		json.NewDecoder(r.Body).Decode(&body)
		name := r.PathValue("name")
		models[name] = model{Name: name, URL: body["url"], HasKey: body["key"] != ""}
		if current == "" {
			current = name
		}
		w.WriteHeader(http.StatusNoContent)
	})
	mux.HandleFunc("DELETE /settings/models/{name...}", func(w http.ResponseWriter, r *http.Request) {
		if !authorized(w, r) {
			return
		}
		name := r.PathValue("name")
		if _, ok := models[name]; !ok {
			w.WriteHeader(http.StatusNotFound)
			return
		}
		delete(models, name)
		if current == name {
			current = ""
		}
		w.WriteHeader(http.StatusNoContent)
	})
	mux.HandleFunc("PUT /settings/current-model", func(w http.ResponseWriter, r *http.Request) {
		if !authorized(w, r) {
			return
		}
		var body map[string]string
		json.NewDecoder(r.Body).Decode(&body)
		if _, ok := models[body["name"]]; !ok {
			w.WriteHeader(http.StatusNotFound)
			return
		}
		current = body["name"]
		w.WriteHeader(http.StatusNoContent)
	})
	mux.HandleFunc("GET /settings", func(w http.ResponseWriter, r *http.Request) {
		if !authorized(w, r) {
			return
		}
		list := []model{}
		for _, m := range models {
			m.Current = m.Name == current
			list = append(list, m)
		}
		sort.Slice(list, func(i, j int) bool { return list[i].Name < list[j].Name })
		json.NewEncoder(w).Encode(map[string]any{"models": list})
	})
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
