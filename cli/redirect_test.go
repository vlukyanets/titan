package main

import (
	"bytes"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
)

// TestNoRedirectIsFollowed: on a 307 Go would send the body again, and
// with it a code or a model key, to the host the redirect names.
func TestNoRedirectIsFollowed(t *testing.T) {
	var stolen atomic.Int32
	thief := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		stolen.Add(1)
	}))
	defer thief.Close()
	node := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, thief.URL+r.URL.Path, http.StatusTemporaryRedirect)
	}))
	defer node.Close()
	configDir(t)
	if err := save(session{URL: node.URL, Token: "t0ken"}); err != nil {
		t.Fatal(err)
	}

	for _, c := range []struct {
		args  []string
		stdin string
	}{
		{[]string{"login", node.URL}, "alice\nc0de\n"},
		{[]string{"settings", "model", "add"}, "https://models.example/v1\nbig\nsk-secret\n"},
	} {
		var stdout, stderr bytes.Buffer
		if code := run(c.args, strings.NewReader(c.stdin), &stdout, &stderr); code != 1 {
			t.Errorf("%v: exit code = %d, want 1", c.args, code)
		}
	}
	if n := stolen.Load(); n != 0 {
		t.Errorf("the redirect was followed %d times", n)
	}
}
