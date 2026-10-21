package main

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

// chatNode saves a session for a node whose POST /chat is handle.
func chatNode(t *testing.T, handle http.HandlerFunc) {
	t.Helper()
	node := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost || r.URL.Path != "/chat" || r.Header.Get("Authorization") != "Bearer t0ken" {
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		handle(w, r)
	}))
	t.Cleanup(node.Close)
	configDir(t)
	if err := save(session{URL: node.URL, Token: "t0ken"}); err != nil {
		t.Fatal(err)
	}
}

// echo answers "you said: <message>" in two chunks.
func echo(w http.ResponseWriter, r *http.Request) {
	var body map[string]string
	json.NewDecoder(r.Body).Decode(&body)
	w.Write([]byte("you said: "))
	w.(http.Flusher).Flush()
	w.Write([]byte(body["message"]))
}

// firstWrite tells when the CLI printed something. It does not embed the
// buffer: io.Copy would call its ReadFrom and skip Write.
type firstWrite struct {
	out     bytes.Buffer
	printed chan struct{}
}

func (w *firstWrite) Write(p []byte) (int, error) {
	if w.out.Len() == 0 {
		close(w.printed)
	}
	return w.out.Write(p)
}

func TestChatPrintsReplyAsItComes(t *testing.T) {
	stdout := &firstWrite{printed: make(chan struct{})}
	// The node sends the rest only after the CLI printed the first chunk.
	chatNode(t, func(w http.ResponseWriter, r *http.Request) {
		w.Write([]byte("Hel"))
		w.(http.Flusher).Flush()
		select {
		case <-stdout.printed:
		case <-time.After(5 * time.Second):
			t.Error("the CLI printed nothing until the reply ended")
		}
		w.Write([]byte("lo"))
	})
	var stderr bytes.Buffer
	if code := run([]string{"chat", "Hi"}, strings.NewReader(""), stdout, &stderr); code != 0 {
		t.Fatalf("exit code = %d, stderr = %q", code, stderr.String())
	}
	if stdout.out.String() != "Hello\n" {
		t.Errorf("stdout = %q, want %q", stdout.out.String(), "Hello\n")
	}
}

func TestChatLoopSendsEachLine(t *testing.T) {
	chatNode(t, echo)
	var stdout, stderr bytes.Buffer
	if code := run([]string{"chat"}, strings.NewReader("Hi\n\nBye\n"), &stdout, &stderr); code != 0 {
		t.Fatalf("exit code = %d, stderr = %q", code, stderr.String())
	}
	if want := "you said: Hi\nyou said: Bye\n"; stdout.String() != want {
		t.Errorf("stdout = %q, want %q", stdout.String(), want)
	}
	// Input from a pipe gets no prompts.
	if stderr.String() != "" {
		t.Errorf("stderr = %q, want nothing", stderr.String())
	}
}

func TestChatLoopTakesLongLines(t *testing.T) {
	chatNode(t, echo)
	long := strings.Repeat("a", 100_000)
	var stdout, stderr bytes.Buffer
	// The last line has no newline; it is sent all the same.
	if code := run([]string{"chat"}, strings.NewReader(long+"\nend"), &stdout, &stderr); code != 0 {
		t.Fatalf("exit code = %d, stderr = %q", code, stderr.String())
	}
	if want := "you said: " + long + "\nyou said: end\n"; stdout.String() != want {
		t.Errorf("stdout has %d bytes, want %d", stdout.Len(), len(want))
	}
}

func TestEmptyMessageIsNotSent(t *testing.T) {
	chatNode(t, func(w http.ResponseWriter, r *http.Request) {
		t.Error("an empty message was sent")
	})
	var stdout, stderr bytes.Buffer
	if code := run([]string{"chat", "  "}, strings.NewReader(""), &stdout, &stderr); code != 1 {
		t.Errorf("exit code = %d, want 1", code)
	}
	if !strings.Contains(stderr.String(), "the message is empty") {
		t.Errorf("stderr = %q", stderr.String())
	}
}

func TestNodeErrorShowsItsReason(t *testing.T) {
	for _, c := range []struct {
		status int
		body   string
		want   string
	}{
		{http.StatusBadGateway, `{"detail":"the model answered 401"}`,
			"titan: the node answered 502 Bad Gateway: the model answered 401\n"},
		// A 422 lists the problems; the first one is shown.
		{http.StatusUnprocessableEntity, `{"detail":[{"loc":["body","url"],"msg":"Value error, bad URL"}]}`,
			"titan: the node answered 422 Unprocessable Entity: Value error, bad URL\n"},
		{http.StatusInternalServerError, `Internal Server Error`,
			"titan: the node answered 500 Internal Server Error\n"},
	} {
		chatNode(t, func(w http.ResponseWriter, r *http.Request) {
			w.WriteHeader(c.status)
			w.Write([]byte(c.body))
		})
		var stdout, stderr bytes.Buffer
		run([]string{"chat", "Hi"}, strings.NewReader(""), &stdout, &stderr)
		if stderr.String() != c.want {
			t.Errorf("stderr = %q, want %q", stderr.String(), c.want)
		}
	}
}

func TestChatWithoutModelSaysHowToAddOne(t *testing.T) {
	chatNode(t, func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusConflict)
	})
	var stdout, stderr bytes.Buffer
	if code := run([]string{"chat", "Hi"}, strings.NewReader(""), &stdout, &stderr); code != 1 {
		t.Errorf("exit code = %d, want 1", code)
	}
	if !strings.Contains(stderr.String(), "run titan settings model add") {
		t.Errorf("stderr = %q", stderr.String())
	}
}

func TestCutReplyIsAnError(t *testing.T) {
	chatNode(t, func(w http.ResponseWriter, r *http.Request) {
		w.Write([]byte("Hel"))
		w.(http.Flusher).Flush()
		panic(http.ErrAbortHandler)
	})
	var stdout, stderr bytes.Buffer
	if code := run([]string{"chat", "Hi"}, strings.NewReader(""), &stdout, &stderr); code != 1 {
		t.Errorf("exit code = %d, want 1", code)
	}
	if !strings.Contains(stderr.String(), "the reply was cut off") {
		t.Errorf("stderr = %q", stderr.String())
	}
}
