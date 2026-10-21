package main

import (
	"bufio"
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"net/http"
	"os"
	"path/filepath"
	"strings"

	"golang.org/x/term"
)

// client follows no redirect: on a 307 Go would send the body again, and
// with it a code or a model key, to whatever host the redirect names.
var client = &http.Client{
	CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
}

// session is what login saves: the node and the token it gave.
type session struct {
	URL   string `json:"url"`
	Token string `json:"token"`
}

func sessionPath() (string, error) {
	dir, err := os.UserConfigDir()
	if err != nil {
		return "", err
	}
	return filepath.Join(dir, "titan", "token"), nil
}

// login asks for the name and the code, signs in and saves the token.
func login(nodeURL string, stdin io.Reader, prompts io.Writer) error {
	in := bufio.NewScanner(stdin)
	name := ask(in, prompts, "Name: ")
	code, err := askSecret(in, stdin, prompts, "Code: ")
	if err != nil {
		return err
	}
	body, err := json.Marshal(map[string]string{"name": name, "code": code})
	if err != nil {
		return err
	}
	nodeURL = strings.TrimRight(nodeURL, "/")
	resp, err := client.Post(nodeURL+"/sign-in", "application/json", bytes.NewReader(body))
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode == http.StatusUnauthorized {
		return errors.New("the node refused the name or the code")
	}
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("sign-in failed: %s", resp.Status)
	}
	var reply struct {
		Token string `json:"token"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&reply); err != nil {
		return err
	}
	return save(session{URL: nodeURL, Token: reply.Token})
}

func ask(in *bufio.Scanner, prompts io.Writer, prompt string) string {
	fmt.Fprint(prompts, prompt)
	in.Scan()
	return strings.TrimSpace(in.Text())
}

func isTerminal(r io.Reader) bool {
	f, ok := r.(*os.File)
	return ok && term.IsTerminal(int(f.Fd()))
}

// askSecret reads a line without echo when stdin is a terminal.
func askSecret(in *bufio.Scanner, stdin io.Reader, prompts io.Writer, prompt string) (string, error) {
	if !isTerminal(stdin) {
		return ask(in, prompts, prompt), nil
	}
	fmt.Fprint(prompts, prompt)
	secret, err := term.ReadPassword(int(stdin.(*os.File).Fd()))
	fmt.Fprintln(prompts)
	return strings.TrimSpace(string(secret)), err
}

// save writes the session to a file only the user can read. It writes a
// new file and renames it, so an old file with wider permissions is
// replaced, not reused.
func save(s session) error {
	path, err := sessionPath()
	if err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return err
	}
	f, err := os.CreateTemp(filepath.Dir(path), "token-*") // mode 0600
	if err != nil {
		return err
	}
	defer os.Remove(f.Name()) // fails harmlessly after the rename
	if err := json.NewEncoder(f).Encode(s); err != nil {
		f.Close()
		return err
	}
	if err := f.Close(); err != nil {
		return err
	}
	return os.Rename(f.Name(), path)
}

func load() (session, error) {
	var s session
	path, err := sessionPath()
	if err != nil {
		return s, err
	}
	data, err := os.ReadFile(path)
	if errors.Is(err, fs.ErrNotExist) {
		return s, errors.New("not signed in; run titan login <node-url>")
	}
	if err != nil {
		return s, err
	}
	return s, json.Unmarshal(data, &s)
}

var (
	errNotFound = errors.New("not found")
	errConflict = errors.New("conflict")
)

// send sends a request with the saved token, and body as JSON unless it is
// nil. It returns the answer only when the node accepted the request; the
// caller closes its body.
func send(method, path string, body any) (*http.Response, error) {
	s, err := load()
	if err != nil {
		return nil, err
	}
	var reader io.Reader
	if body != nil {
		data, err := json.Marshal(body)
		if err != nil {
			return nil, err
		}
		reader = bytes.NewReader(data)
	}
	req, err := http.NewRequest(method, s.URL+path, reader)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Authorization", "Bearer "+s.Token)
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	resp, err := client.Do(req)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode/100 == 2 {
		return resp, nil
	}
	defer resp.Body.Close()
	switch resp.StatusCode {
	case http.StatusUnauthorized:
		return nil, errors.New("the node refused the saved token; run titan login <node-url>")
	case http.StatusNotFound:
		return nil, errNotFound
	case http.StatusConflict:
		return nil, errConflict
	}
	if reason := reasonOf(resp.Body); reason != "" {
		return nil, fmt.Errorf("the node answered %s: %s", resp.Status, reason)
	}
	return nil, fmt.Errorf("the node answered %s", resp.Status)
}

// reasonOf reads the reason from the node's detail: text, or for a 422 a
// list of problems, of which it takes the first.
func reasonOf(body io.Reader) string {
	var reply struct {
		Detail json.RawMessage `json:"detail"`
	}
	json.NewDecoder(body).Decode(&reply)
	var text string
	if json.Unmarshal(reply.Detail, &text) == nil {
		return text
	}
	var problems []struct {
		Msg string `json:"msg"`
	}
	if json.Unmarshal(reply.Detail, &problems) == nil && len(problems) > 0 {
		return problems[0].Msg
	}
	return ""
}

// call sends a request as send does and decodes the answer into out unless
// out is nil.
func call(method, path string, body, out any) error {
	resp, err := send(method, path, body)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if out != nil {
		return json.NewDecoder(resp.Body).Decode(out)
	}
	return nil
}

// whoami prints the name the saved token belongs to.
func whoami(stdout io.Writer) error {
	var me struct {
		Name string `json:"name"`
	}
	if err := call(http.MethodGet, "/me", nil, &me); err != nil {
		return err
	}
	fmt.Fprintln(stdout, me.Name)
	return nil
}
